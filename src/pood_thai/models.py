"""Shared types and service interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Lang = Literal["th", "en"]
Role = Literal["user", "assistant"]
Action = Literal["reply", "repeat", "repeat_slower"]
PartName = Literal["note", "thai"]

DEFAULT_VOICE = "premwadee"


@dataclass(frozen=True)
class Message:
    id: int
    session_id: int
    role: Role
    lang: Lang
    thai: str
    english: str
    english_note: str
    created_at: str


@dataclass(frozen=True)
class Fact:
    id: int
    fact: str
    source_session_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Turn:
    """One parsed reply from Claude."""

    action: Action
    user_english: str
    english_note: str
    thai: str
    english: str


@dataclass(frozen=True)
class Segment:
    """A piece of text to speak in one language, and where it sits in the message (for word timings)."""

    text: str
    lang: Lang
    part: PartName = "thai"
    offset: int = 0  # index of `text` within the message's english_note (part "note") or thai (part "thai")


@dataclass(frozen=True)
class Word:
    """One spoken word: its character range within a message part, and when it is heard (seconds)."""

    part: PartName
    start: int
    end: int
    t_start: float
    t_end: float


@dataclass(frozen=True)
class Audio:
    data: bytes
    mime: str
    fallback_used: bool = False
    words: tuple[Word, ...] = ()


@dataclass(frozen=True)
class ReviewItem:
    """One Thai sentence the learner said, and (when it could be more natural) a better version."""

    said: str
    ok: bool
    better: str = ""
    why: str = ""


@dataclass(frozen=True)
class Review:
    id: int
    session_id: int
    topic: str
    created_at: str
    items: tuple[ReviewItem, ...]
    tips: tuple[str, ...]


class BrainError(Exception):
    """Claude could not produce a usable reply."""


class SpeechError(Exception):
    """Text-to-speech failed."""


class TranscriptionError(Exception):
    """Speech-to-text failed (bad audio, missing ffmpeg, model load error)."""


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, suffix: str, lang: Lang) -> str: ...


class Speaker(Protocol):
    async def synthesize(self, segments: list[Segment], voice: str) -> Audio: ...


class Chat(Protocol):
    async def send(self, message: str) -> str: ...

    async def close(self) -> None: ...


class Brain(Protocol):
    async def open_chat(self, system_prompt: str) -> Chat: ...

    async def complete(self, system_prompt: str, message: str) -> str: ...
