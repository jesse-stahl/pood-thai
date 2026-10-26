"""Text-to-speech: Azure neural voices, with macOS `say` as a fallback."""

from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx

from pood_thai.models import Audio, Segment, Speaker, SpeechError

logger = logging.getLogger(__name__)

AZURE_VOICES = {"th": "th-TH-PremwadeeNeural", "en": "en-US-AvaNeural"}
SAY_VOICES = {"th": "Kanya", "en": "Samantha"}
SAY_BASE_WPM = 175


def rate_to_percent(rate: float) -> str:
    return f"{round((rate - 1) * 100):+d}%"


def build_ssml(segments: list[Segment], rate: float) -> str:
    prosody = rate_to_percent(rate)
    voices = "".join(
        f'<voice name="{AZURE_VOICES[s.lang]}"><prosody rate="{prosody}">{escape(s.text)}</prosody></voice>'
        for s in segments
        if s.text.strip()
    )
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="th-TH">'
        f"{voices}</speak>"
    )


class AzureSpeaker:
    def __init__(self, key: str, region: str, client: httpx.AsyncClient | None = None):
        self._key = key
        self._region = region
        self._client = client or httpx.AsyncClient()

    async def synthesize(self, segments: list[Segment], rate: float) -> Audio:
        if not self._key:
            raise SpeechError("AZURE_SPEECH_KEY is not set")
        url = f"https://{self._region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            "User-Agent": "pood-thai",
        }
        try:
            resp = await self._client.post(
                url, content=build_ssml(segments, rate).encode("utf-8"), headers=headers, timeout=15
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"Azure TTS request failed: {exc}") from exc
        if resp.status_code != 200:
            raise SpeechError(f"Azure TTS {resp.status_code}: {resp.text[:200]}")
        return Audio(resp.content, "audio/mpeg")


def concat_wavs(paths: list[Path]) -> bytes:
    buf = io.BytesIO()
    with wave.open(str(paths[0])) as first:
        params = first.getparams()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        for path in paths:
            with wave.open(str(path)) as w:
                out.writeframes(w.readframes(w.getnframes()))
    return buf.getvalue()


class SaySpeaker:
    def __init__(self, run: Callable[..., Any] = subprocess.run):
        self._run = run

    def _synthesize_sync(self, segments: list[Segment], rate: float) -> bytes:
        wpm = str(int(SAY_BASE_WPM * rate))
        with tempfile.TemporaryDirectory() as tmp:
            outputs: list[Path] = []
            for i, segment in enumerate(s for s in segments if s.text.strip()):
                text_file = Path(tmp) / f"{i}.txt"
                text_file.write_text(segment.text, encoding="utf-8")
                out = Path(tmp) / f"{i}.wav"
                self._run(
                    [
                        "say", "-v", SAY_VOICES[segment.lang], "-r", wpm,
                        "-f", str(text_file),
                        "--file-format=WAVE", "--data-format=LEI16@22050",
                        "-o", str(out),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                outputs.append(out)
            if not outputs:
                raise SpeechError("Nothing to speak")
            return concat_wavs(outputs)

    async def synthesize(self, segments: list[Segment], rate: float) -> Audio:
        try:
            data = await asyncio.to_thread(self._synthesize_sync, segments, rate)
        except SpeechError:
            raise
        except Exception as exc:
            raise SpeechError(f"macOS say failed: {exc}") from exc
        return Audio(data, "audio/wav")


class FallbackSpeaker:
    def __init__(self, primary: Speaker, fallback: Speaker):
        self._primary = primary
        self._fallback = fallback

    async def synthesize(self, segments: list[Segment], rate: float) -> Audio:
        try:
            return await self._primary.synthesize(segments, rate)
        except SpeechError as exc:
            logger.warning("Primary TTS failed, using fallback: %s", exc)
            audio = await self._fallback.synthesize(segments, rate)
            return Audio(audio.data, audio.mime, fallback_used=True)
