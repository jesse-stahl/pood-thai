"""Per-session conversation flow: listen, think, speak, undo, remember."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pood_thai.db import Database
from pood_thai.models import Audio, Brain, BrainError, Chat, Fact, Lang, Message, Segment, Speaker, Transcriber
from pood_thai.prompts import (
    MEMORY_SYSTEM_PROMPT,
    SESSION_START,
    build_memory_request,
    build_system_prompt,
    format_user_message,
)
from pood_thai.topics import get_topic
from pood_thai.turns import parse_facts, parse_turn

logger = logging.getLogger(__name__)

DEFAULT_RATE = 0.9
SLOW_RATE = 0.75


class SessionNotFound(KeyError):
    """No live session with that id."""


class NoSpeech(Exception):
    """The recording contained no recognisable speech."""


@dataclass
class _LiveSession:
    session_id: int
    chat: Chat
    rate: float = DEFAULT_RATE
    withdrawn: int = 0  # exchanges undone since Claude last heard from the user
    last_assistant: Message | None = None


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
    audio: Audio


def speech_segments(message: Message) -> list[Segment]:
    segments: list[Segment] = []
    if message.english_note:
        segments.append(Segment(message.english_note, "en"))
    if message.thai:
        segments.append(Segment(message.thai, "th"))
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

    def _get(self, session_id: int) -> _LiveSession:
        live = self._live.get(session_id)
        if live is None:
            raise SessionNotFound(session_id)
        return live

    async def _speak(self, live: _LiveSession, message: Message) -> Audio:
        return await self._speaker.synthesize(speech_segments(message), live.rate)

    async def start(self, topic_id: str) -> StartResult:
        topic = get_topic(topic_id)
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
        return StartResult(session_id, message, await self._speak(live, message))

    async def handle_audio(self, session_id: int, audio: bytes, suffix: str, lang: Lang) -> TurnResult:
        live = self._get(session_id)
        text = await self._transcriber.transcribe(audio, suffix, lang)
        if not text:
            raise NoSpeech()
        turn = parse_turn(await live.chat.send(format_user_message(text, lang, withdrawn=live.withdrawn)))
        live.withdrawn = 0

        if turn.action != "reply":
            if turn.action == "repeat_slower":
                live.rate = SLOW_RATE
            if live.last_assistant is None:
                raise BrainError("Nothing to repeat yet")
            return TurnResult("repeat", None, None, await self._speak(live, live.last_assistant))

        if lang == "th":
            user = self._db.add_message(session_id, "user", "th", thai=text, english=turn.user_english)
        else:
            user = self._db.add_message(session_id, "user", "en", english=text)
        assistant = self._db.add_message(
            session_id, "assistant", "th", thai=turn.thai, english=turn.english, english_note=turn.english_note
        )
        live.last_assistant = assistant
        return TurnResult("reply", user, assistant, await self._speak(live, assistant))

    async def replay(self, session_id: int, slow: bool) -> Audio:
        live = self._get(session_id)
        if slow:
            live.rate = SLOW_RATE
        if live.last_assistant is None:
            raise BrainError("Nothing to replay yet")
        return await self._speak(live, live.last_assistant)

    async def undo(self, session_id: int) -> list[int]:
        live = self._get(session_id)
        removed = self._db.delete_last_exchange(session_id)
        if removed:
            live.withdrawn += 1
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
        return self._db.replace_facts(facts, source_session_id=session_id)

    async def _abandon_live_sessions(self) -> None:
        for session_id, live in list(self._live.items()):
            del self._live[session_id]
            await live.chat.close()
            self._db.end_session(session_id)

    async def close_all(self) -> None:
        await self._abandon_live_sessions()
