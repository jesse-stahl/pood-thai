"""Shared types and service interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Lang = Literal["th", "en"]
Role = Literal["user", "assistant"]
Action = Literal["reply", "repeat", "repeat_slower"]


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
    """A piece of text to speak in one language."""

    text: str
    lang: Lang


@dataclass(frozen=True)
class Audio:
    data: bytes
    mime: str
    fallback_used: bool = False


class BrainError(Exception):
    """Claude could not produce a usable reply."""


class SpeechError(Exception):
    """Text-to-speech failed."""


class TranscriptionError(Exception):
    """Speech-to-text failed (bad audio, missing ffmpeg, model load error)."""


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, suffix: str, lang: Lang) -> str: ...


class Speaker(Protocol):
    async def synthesize(self, segments: list[Segment], rate: float) -> Audio: ...


class Chat(Protocol):
    async def send(self, message: str) -> str: ...

    async def close(self) -> None: ...


class Brain(Protocol):
    async def open_chat(self, system_prompt: str) -> Chat: ...

    async def complete(self, system_prompt: str, message: str) -> str: ...
