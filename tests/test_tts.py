import io
import wave
from pathlib import Path

import httpx
import pytest

from pood_thai.models import Audio, Segment, SpeechError
from pood_thai.tts import (
    AzureSpeaker,
    FallbackSpeaker,
    SaySpeaker,
    build_ssml,
    concat_wavs,
    rate_to_percent,
)


def _wav_bytes(frames: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


def test_rate_to_percent():
    assert rate_to_percent(0.9) == "-10%"
    assert rate_to_percent(0.75) == "-25%"
    assert rate_to_percent(1.0) == "+0%"


def test_build_ssml_uses_voice_per_language_and_escapes_text():
    ssml = build_ssml([Segment("Tired is <เหนื่อย> & more", "en"), Segment("เหนื่อยค่ะ", "th"), Segment("  ", "th")], 0.9)
    assert ssml.startswith("<speak")
    assert ssml.index("en-US-AvaNeural") < ssml.index("th-TH-PremwadeeNeural")
    assert "&lt;เหนื่อย&gt; &amp; more" in ssml
    assert ssml.count("<voice") == 2
    assert 'rate="-10%"' in ssml


async def test_azure_speaker_posts_ssml_and_returns_mp3():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = request.content.decode()
        return httpx.Response(200, content=b"MP3")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    speaker = AzureSpeaker("key123", "southeastasia", client=client)
    audio = await speaker.synthesize([Segment("สวัสดีค่ะ", "th")], 0.9)
    assert audio == Audio(b"MP3", "audio/mpeg")
    assert seen["url"] == "https://southeastasia.tts.speech.microsoft.com/cognitiveservices/v1"
    assert seen["headers"]["ocp-apim-subscription-key"] == "key123"
    assert seen["headers"]["x-microsoft-outputformat"] == "audio-24khz-48kbitrate-mono-mp3"
    assert "สวัสดีค่ะ" in seen["body"]


async def test_azure_speaker_raises_on_http_error_status():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="quota")))
    with pytest.raises(SpeechError, match="429"):
        await AzureSpeaker("k", "r", client=client).synthesize([Segment("ค่ะ", "th")], 1.0)


async def test_azure_speaker_without_key_raises():
    with pytest.raises(SpeechError, match="AZURE_SPEECH_KEY"):
        await AzureSpeaker("", "r").synthesize([Segment("ค่ะ", "th")], 1.0)


def test_concat_wavs_joins_frames(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(_wav_bytes(100))
    b.write_bytes(_wav_bytes(50))
    with wave.open(io.BytesIO(concat_wavs([a, b]))) as w:
        assert w.getnframes() == 150


async def test_say_speaker_runs_say_per_segment_with_voice_and_rate():
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(_wav_bytes(10))
        text_file = Path(cmd[cmd.index("-f") + 1])
        assert text_file.read_text(encoding="utf-8")

    audio = await SaySpeaker(run=fake_run).synthesize([Segment("Hello", "en"), Segment("สวัสดีค่ะ", "th")], 0.9)
    assert audio.mime == "audio/wav"
    assert [c[c.index("-v") + 1] for c in commands] == ["Samantha", "Kanya"]
    assert commands[0][commands[0].index("-r") + 1] == "157"
    with wave.open(io.BytesIO(audio.data)) as w:
        assert w.getnframes() == 20


async def test_say_speaker_wraps_failures():
    def boom(cmd, **kwargs):
        raise OSError("no say")

    with pytest.raises(SpeechError):
        await SaySpeaker(run=boom).synthesize([Segment("ค่ะ", "th")], 1.0)


class _Failing:
    async def synthesize(self, segments, rate):
        raise SpeechError("down")


class _Working:
    async def synthesize(self, segments, rate):
        return Audio(b"WAV", "audio/wav")


async def test_fallback_speaker_uses_fallback_and_flags_it():
    audio = await FallbackSpeaker(_Failing(), _Working()).synthesize([Segment("ค่ะ", "th")], 1.0)
    assert audio == Audio(b"WAV", "audio/wav", fallback_used=True)


async def test_fallback_speaker_prefers_primary():
    audio = await FallbackSpeaker(_Working(), _Failing()).synthesize([Segment("ค่ะ", "th")], 1.0)
    assert audio.fallback_used is False
