"""In-memory fakes for the service Protocols in pood_thai.models."""

from __future__ import annotations

import json
from collections.abc import Callable

from pood_thai.models import Audio, Lang, Segment, Word


class FakeTranscriber:
    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.calls: list[tuple[bytes, str, Lang]] = []

    async def transcribe(self, audio: bytes, suffix: str, lang: Lang) -> str:
        self.calls.append((audio, suffix, lang))
        return self.texts.pop(0)


class FakeSpeaker:
    """Speaks each segment as one 'word' lasting 0.5 s, so word timings flow through like Azure's."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Segment], str]] = []

    async def synthesize(self, segments: list[Segment], voice: str) -> Audio:
        self.calls.append((list(segments), voice))
        words = tuple(
            Word(s.part, s.offset, s.offset + len(s.text), i * 0.5, i * 0.5 + 0.5) for i, s in enumerate(segments)
        )
        return Audio(data=("AUDIO:" + "|".join(s.text for s in segments)).encode(), mime="audio/mpeg", words=words)


class FakeChat:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> str:
        self.sent.append(message)
        return self.replies.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeBrain:
    """open_chat() returns a fresh FakeChat per call, fed from `chat_replies` in order."""

    def __init__(self, chat_replies: list[list[str]], completion: "str | Callable[[str, str], str]" = '{"facts": []}'):
        self._chat_replies = list(chat_replies)
        self.chats: list[FakeChat] = []
        self.system_prompts: list[str] = []
        self.completion = completion
        self.complete_calls: list[tuple[str, str]] = []

    async def open_chat(self, system_prompt: str) -> FakeChat:
        self.system_prompts.append(system_prompt)
        chat = FakeChat(self._chat_replies.pop(0))
        self.chats.append(chat)
        return chat

    async def complete(self, system_prompt: str, message: str) -> str:
        self.complete_calls.append((system_prompt, message))
        # A callable completion answers per prompt (e.g. memory vs review) instead of one fixed reply.
        return self.completion(system_prompt, message) if callable(self.completion) else self.completion


def reply_json(
    thai: str = "สวัสดีค่ะ",
    english: str = "Hello",
    action: str = "reply",
    user_english: str = "",
    english_note: str = "",
) -> str:
    return json.dumps(
        {
            "action": action,
            "user_english": user_english,
            "english_note": english_note,
            "thai": thai,
            "english": english,
        },
        ensure_ascii=False,
    )
