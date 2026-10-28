"""Text-to-speech: Azure neural voices with word timings, and macOS `say` as a fallback."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import subprocess
import tempfile
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from pood_thai.models import Audio, Segment, Speaker, SpeechError, Word

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Voice:
    id: str
    label: str
    azure_name: str
    multilingual: bool = False  # speaks Thai and English itself, so no switching to the English voice


VOICES: dict[str, Voice] = {
    v.id: v
    for v in (
        Voice("premwadee", "Premwadee · Thai, female", "th-TH-PremwadeeNeural"),
        Voice("achara", "Achara · Thai, female", "th-TH-AcharaNeural"),
        Voice("niwat", "Niwat · Thai, male", "th-TH-NiwatNeural"),
        Voice("ava-multi", "Ava · multilingual, female", "en-US-AvaMultilingualNeural", multilingual=True),
    )
}
ENGLISH_VOICE = "en-US-AvaNeural"  # reads English help notes next to the Thai-only voices
SAY_VOICES = {"th": "Kanya", "en": "Samantha"}
AZURE_TIMEOUT_S = 15  # a hung request falls back to the Mac voice instead of blocking the turn
# A timed-out Azure call keeps its thread until the SDK gives up, so Azure gets its own small pool and
# can never starve the default pool the Mac fallback (asyncio.to_thread) runs on.
SDK_FRAME_TIMEOUT_MS = 10_000  # max gap between audio frames before the SDK cancels on its own
_AZURE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="azure-tts")
_SPEAK_OPEN = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="th-TH">'


@dataclass(frozen=True)
class Boundary:
    """An Azure word-boundary event. text_offset indexes the SSML string; times are seconds."""

    kind: str  # "Word", "Punctuation" or "Sentence"
    text: str
    text_offset: int
    word_length: int
    start: float
    duration: float


@dataclass(frozen=True)
class SsmlSpan:
    """Where one segment's escaped text sits in the SSML; index_map maps each escaped char to a raw char."""

    start: int
    index_map: tuple[int, ...]
    segment: Segment


def _escape_with_map(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        escaped = escape(ch)
        out.append(escaped)
        index_map.extend([i] * len(escaped))
    return "".join(out), index_map


def build_ssml(segments: list[Segment], voice_id: str) -> tuple[str, list[SsmlSpan]]:
    voice = VOICES[voice_id]
    parts: list[str] = []
    spans: list[SsmlSpan] = []
    length = 0

    def emit(markup: str) -> None:
        nonlocal length
        parts.append(markup)
        length += len(markup)

    def emit_text(segment: Segment) -> None:
        escaped, index_map = _escape_with_map(segment.text)
        spans.append(SsmlSpan(length, tuple(index_map), segment))
        emit(escaped)

    speakable = [s for s in segments if s.text.strip()]
    emit(_SPEAK_OPEN)
    if voice.multilingual:
        emit(f'<voice name="{voice.azure_name}">')
        for i, segment in enumerate(speakable):
            if i:
                emit(" ")
            if segment.lang == "th":
                emit('<lang xml:lang="th-TH">')
                emit_text(segment)
                emit("</lang>")
            else:
                emit_text(segment)
        emit("</voice>")
    else:
        for segment in speakable:
            name = voice.azure_name if segment.lang == "th" else ENGLISH_VOICE
            emit(f'<voice name="{name}">')
            emit_text(segment)
            emit("</voice>")
    emit("</speak>")
    return "".join(parts), spans


def _locate_by_offset(spans: list[SsmlSpan], boundary: Boundary) -> tuple[int, int, int] | None:
    for i, span in enumerate(spans):
        rel = boundary.text_offset - span.start
        if 0 <= rel and rel + boundary.word_length <= len(span.index_map):
            raw_start = span.index_map[rel]
            raw_end = span.index_map[rel + boundary.word_length - 1] + 1
            if span.segment.text[raw_start:raw_end] == boundary.text:
                return i, raw_start, raw_end
            return None
    return None


def _locate_by_search(
    spans: list[SsmlSpan], boundary: Boundary, cursor: tuple[int, int]
) -> tuple[int, int, int] | None:
    first, pos = cursor
    for i in range(first, len(spans)):
        found = spans[i].segment.text.find(boundary.text, pos if i == first else 0)
        if found != -1:
            return i, found, found + len(boundary.text)
    return None


def map_words(spans: list[SsmlSpan], boundaries: list[Boundary]) -> tuple[Word, ...]:
    """Turn Azure word boundaries into character ranges of the message parts, in speaking order."""
    words: list[Word] = []
    cursor = (0, 0)
    for boundary in boundaries:
        if boundary.kind != "Word" or boundary.word_length <= 0 or not boundary.text:
            continue
        # Offsets are exact when they match; text search covers anything that shifts them.
        located = _locate_by_offset(spans, boundary) or _locate_by_search(spans, boundary, cursor)
        if located is None:
            continue
        i, raw_start, raw_end = located
        segment = spans[i].segment
        words.append(
            Word(
                segment.part,
                segment.offset + raw_start,
                segment.offset + raw_end,
                round(boundary.start, 3),
                round(boundary.start + boundary.duration, 3),
            )
        )
        cursor = (i, raw_end)
    return tuple(words)


def _azure_synthesize(key: str, region: str, ssml: str) -> tuple[bytes, list[Boundary]]:
    import azure.cognitiveservices.speech as speechsdk  # native library; load only when Azure is used

    config = speechsdk.SpeechConfig(subscription=key, region=region)
    config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm)
    # Make the SDK itself give up on a stalled stream, so a timed-out call's thread comes back (and Ctrl+C
    # isn't held up waiting for it) instead of lingering after AZURE_TIMEOUT_S.
    config.set_property(speechsdk.PropertyId.SpeechSynthesis_FrameTimeoutInterval, str(SDK_FRAME_TIMEOUT_MS))
    # audio_config=None keeps the audio in memory instead of playing it on this machine.
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=config, audio_config=None)
    boundaries: list[Boundary] = []
    synthesizer.synthesis_word_boundary.connect(
        lambda e: boundaries.append(
            Boundary(
                str(e.boundary_type).rsplit(".", 1)[-1],
                e.text,
                e.text_offset,
                e.word_length,
                e.audio_offset / 10_000_000,
                e.duration.total_seconds(),
            )
        )
    )
    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = result.cancellation_details
        raise SpeechError(f"Azure TTS failed ({details.reason}): {(details.error_details or '')[:200]}")
    return result.audio_data, boundaries


class AzureSpeaker:
    def __init__(
        self,
        key: str,
        region: str,
        synth_fn: Callable[[str], tuple[bytes, list[Boundary]]] | None = None,
    ):
        self._key = key
        self._synth_fn = synth_fn or functools.partial(_azure_synthesize, key, region)

    async def synthesize(self, segments: list[Segment], voice: str) -> Audio:
        if not self._key:
            raise SpeechError("AZURE_SPEECH_KEY is not set")
        if voice not in VOICES:
            raise SpeechError(f"Unknown voice {voice!r}")
        ssml, spans = build_ssml(segments, voice)
        if not spans:
            raise SpeechError("Nothing to speak")
        try:
            loop = asyncio.get_running_loop()
            data, boundaries = await asyncio.wait_for(
                loop.run_in_executor(_AZURE_POOL, self._synth_fn, ssml), AZURE_TIMEOUT_S
            )
        except SpeechError:
            raise
        except TimeoutError as exc:
            raise SpeechError(f"Azure TTS timed out after {AZURE_TIMEOUT_S}s") from exc
        except Exception as exc:
            raise SpeechError(f"Azure TTS request failed: {exc}") from exc
        return Audio(data, "audio/wav", words=map_words(spans, boundaries))


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
    """The Mac's built-in voices. One voice per language, and no word timings."""

    def __init__(self, run: Callable[..., Any] = subprocess.run):
        self._run = run

    def _synthesize_sync(self, segments: list[Segment]) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            outputs: list[Path] = []
            for i, segment in enumerate(s for s in segments if s.text.strip()):
                text_file = Path(tmp) / f"{i}.txt"
                text_file.write_text(segment.text, encoding="utf-8")
                out = Path(tmp) / f"{i}.wav"
                self._run(
                    [
                        "say", "-v", SAY_VOICES[segment.lang],
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

    async def synthesize(self, segments: list[Segment], voice: str) -> Audio:
        try:
            data = await asyncio.to_thread(self._synthesize_sync, segments)
        except SpeechError:
            raise
        except Exception as exc:
            raise SpeechError(f"macOS say failed: {exc}") from exc
        return Audio(data, "audio/wav")


class FallbackSpeaker:
    def __init__(self, primary: Speaker, fallback: Speaker):
        self._primary = primary
        self._fallback = fallback

    async def synthesize(self, segments: list[Segment], voice: str) -> Audio:
        try:
            return await self._primary.synthesize(segments, voice)
        except SpeechError as exc:
            logger.warning("Primary TTS failed, using fallback: %s", exc)
            audio = await self._fallback.synthesize(segments, voice)
            return Audio(audio.data, audio.mime, fallback_used=True, words=audio.words)
