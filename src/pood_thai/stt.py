"""Local speech-to-text with mlx-whisper."""

from __future__ import annotations

import asyncio
import io
import tempfile
import wave
from collections.abc import Callable
from typing import Any

from pood_thai.models import Lang, TranscriptionError

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# Phrases Whisper tends to invent on silence or noise (it was trained on subtitled video).
_SILENCE_HALLUCINATIONS = (
    "ขอบคุณที่รับชม",
    "ขอบคุณที่ติดตามชม",
    "ซับไตเติ้ลโดย",
    "thank you for watching",
    "thanks for watching",
)


def clean_transcript(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.lower()
    if len(stripped) < 40 and any(h in lowered for h in _SILENCE_HALLUCINATIONS):
        return ""
    return stripped


def silent_wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class MlxWhisperTranscriber:
    def __init__(self, model: str = DEFAULT_MODEL, transcribe_fn: Callable[..., dict[str, Any]] | None = None):
        self._model = model
        self._transcribe_fn = transcribe_fn
        self._lock = asyncio.Lock()

    def _fn(self) -> Callable[..., dict[str, Any]]:
        if self._transcribe_fn is None:
            import mlx_whisper  # heavy import; load only when first needed

            self._transcribe_fn = mlx_whisper.transcribe
        return self._transcribe_fn

    def _run(self, audio: bytes, suffix: str, lang: Lang) -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(audio)
            f.flush()
            result = self._fn()(
                f.name,
                path_or_hf_repo=self._model,
                language=lang,
                condition_on_previous_text=False,
                verbose=None,
            )
        return clean_transcript(str(result.get("text", "")))

    async def transcribe(self, audio: bytes, suffix: str, lang: Lang) -> str:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._run, audio, suffix, lang)
            except Exception as exc:
                raise TranscriptionError(f"Speech recognition failed: {exc}") from exc

    async def warm_up(self) -> None:
        """Download and load the model so the first real turn is fast."""
        await self.transcribe(silent_wav(0.5), ".wav", "th")
