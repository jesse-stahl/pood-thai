"""Per-session conversation flow: listen, think, speak, undo, remember."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pood_thai.db import Database
from pood_thai.models import (
    DEFAULT_VOICE,
    Audio,
    Brain,
    BrainError,
    Chat,
    Fact,
    Lang,
    Message,
    Segment,
    Speaker,
    Transcriber,
)
from pood_thai.prompts import (
    MEMORY_SYSTEM_PROMPT,
    SESSION_START,
    build_memory_request,
    build_system_prompt,
    format_user_message,
)
from pood_thai.topics import Topic, get_topic
from pood_thai.turns import parse_facts, parse_turn

logger = logging.getLogger(__name__)


class SessionNotFound(KeyError):
    """No live session with that id."""


class MessageNotFound(KeyError):
    """No reply from Ploy with that id in the session."""


class NoSpeech(Exception):
    """The recording contained no recognisable speech."""


@dataclass
class _LiveSession:
    session_id: int
    chat: Chat
    withdrawn: int = 0  # most recent user messages Claude must ignore, sent with the next message
    last_assistant: Message | None = None
    # One entry per user message Claude has seen: its stored message id, or None for a repeat request.
    history: list[int | None] = field(default_factory=list)


@dataclass(frozen=True)
class StartResult:
    session_id: int
    message: Message
    audio: Audio


@dataclass(frozen=True)
class TurnResult:
    kind: Literal["reply", "repeat"]
    user_message: Message | None
    assistant_message: Message | None
    audio: Audio | None  # None for "repeat": the browser replays the clip it already has
    replay_message_id: int | None = None


# Thai words (and the spaces between them) inside an English help note.
_THAI_RUN = re.compile(r"[฀-๿]+(?:\s+[฀-๿]+)*")


def _note_piece(note: str, start: int, end: int, lang: Lang) -> Segment | None:
    raw = note[start:end]
    text = raw.strip()
    if lang == "en" and not any(c.isalnum() for c in text):
        return None
    return Segment(text, lang, "note", start + len(raw) - len(raw.lstrip()))


def _split_note(note: str) -> list[Segment]:
    """English voices skip Thai script, so give each Thai run in a note to the Thai voice."""
    pieces: list[Segment | None] = []
    pos = 0
    for match in _THAI_RUN.finditer(note):
        pieces.append(_note_piece(note, pos, match.start(), "en"))
        pieces.append(_note_piece(note, match.start(), match.end(), "th"))
        pos = match.end()
    pieces.append(_note_piece(note, pos, len(note), "en"))
    return [p for p in pieces if p is not None]


def speech_segments(message: Message) -> list[Segment]:
    segments: list[Segment] = []
    if message.english_note:
        segments.extend(_split_note(message.english_note))
    if message.thai:
        segments.append(Segment(message.thai, "th", "thai", 0))
    return segments


class ConversationManager:
    def __init__(
        self,
        db: Database,
        brain: Brain,
        transcriber: Transcriber,
        speaker: Speaker,
        today: Callable[[], str] = lambda: date.today().isoformat(),
    ):
        self._db = db
        self._brain = brain
        self._transcriber = transcriber
        self._speaker = speaker
        self._today = today
        self._live: dict[int, _LiveSession] = {}
        self._start_lock = asyncio.Lock()

    def _get(self, session_id: int) -> _LiveSession:
        live = self._live.get(session_id)
        if live is None:
            raise SessionNotFound(session_id)
        return live

    async def _speak(self, message: Message, voice: str) -> Audio:
        return await self._speaker.synthesize(speech_segments(message), voice)

    async def start(self, topic_id: str, voice: str = DEFAULT_VOICE) -> StartResult:
        topic = get_topic(topic_id)
        async with self._start_lock:  # a double-clicked topic must not leave two live sessions
            return await self._start(topic, voice)

    async def _start(self, topic: Topic, voice: str) -> StartResult:
        await self._abandon_live_sessions()
        facts = [f.fact for f in self._db.list_facts()]
        chat = await self._brain.open_chat(build_system_prompt(topic, facts))
        try:
            turn = parse_turn(await chat.send(SESSION_START))
        except Exception:
            await chat.close()
            raise
        session_id = self._db.create_session(topic.id)
        live = _LiveSession(session_id=session_id, chat=chat)
        self._live[session_id] = live
        message = self._db.add_message(
            session_id, "assistant", "th", thai=turn.thai, english=turn.english, english_note=turn.english_note
        )
        live.last_assistant = message
        return StartResult(session_id, message, await self._speak(message, voice))

    async def handle_audio(
        self, session_id: int, audio: bytes, suffix: str, lang: Lang, voice: str = DEFAULT_VOICE
    ) -> TurnResult:
        live = self._get(session_id)
        text = await self._transcriber.transcribe(audio, suffix, lang)
        if not text:
            raise NoSpeech()
        raw = await live.chat.send(format_user_message(text, lang, withdrawn=live.withdrawn))
        # Claude has now seen this message, even if its reply turns out to be unusable.
        live.withdrawn = 0
        live.history.append(None)
        turn = parse_turn(raw)

        if turn.action != "reply":
            # Slow-down and repeat requests both just replay; speed is the browser's slider.
            if live.last_assistant is None:
                raise BrainError("Nothing to repeat yet")
            return TurnResult("repeat", None, None, None, replay_message_id=live.last_assistant.id)

        if lang == "th":
            user = self._db.add_message(session_id, "user", "th", thai=text, english=turn.user_english)
        else:
            user = self._db.add_message(session_id, "user", "en", english=text)
        assistant = self._db.add_message(
            session_id, "assistant", "th", thai=turn.thai, english=turn.english, english_note=turn.english_note
        )
        live.history[-1] = user.id
        live.last_assistant = assistant
        return TurnResult("reply", user, assistant, await self._speak(assistant, voice))

    async def speak(self, session_id: int, message_id: int, voice: str) -> Audio:
        """Re-render one of Ploy's replies in this session, e.g. in another voice."""
        self._get(session_id)
        for message in self._db.list_messages(session_id):
            if message.id == message_id and message.role == "assistant":
                return await self._speak(message, voice)
        raise MessageNotFound(message_id)

    async def undo(self, session_id: int) -> list[int]:
        live = self._get(session_id)
        removed = self._db.delete_last_exchange(session_id)
        if removed:
            # Withdraw the undone user message and everything Claude heard after it (e.g. repeat requests).
            user_id = removed[0]
            if user_id in live.history:
                live.withdrawn = len(live.history) - live.history.index(user_id)
            assistants = [m for m in self._db.list_messages(session_id) if m.role == "assistant"]
            live.last_assistant = assistants[-1] if assistants else None
        return removed

    async def end(self, session_id: int) -> list[Fact]:
        live = self._get(session_id)
        del self._live[session_id]
        await live.chat.close()
        self._db.end_session(session_id)
        messages = self._db.list_messages(session_id)
        current = self._db.list_facts()
        if not any(m.role == "user" for m in messages):
            return current
        try:
            raw = await self._brain.complete(
                MEMORY_SYSTEM_PROMPT,
                build_memory_request(messages, [f.fact for f in current], self._today()),
            )
            facts = parse_facts(raw)
        except BrainError:
            logger.warning("Memory extraction failed; keeping existing facts", exc_info=True)
            return current
        if not facts and current:
            logger.warning("Memory extraction returned no facts; keeping existing facts")
            return current
        return self._db.replace_facts(facts, source_session_id=session_id)

    async def _abandon_live_sessions(self) -> None:
        for session_id, live in list(self._live.items()):
            del self._live[session_id]
            await live.chat.close()
            self._db.end_session(session_id)

    async def close_all(self) -> None:
        await self._abandon_live_sessions()
