import io
import wave
from pathlib import Path

from pood_thai.stt import MlxWhisperTranscriber, clean_transcript, silent_wav


def test_transcribe_writes_temp_file_with_suffix_and_passes_language():
    seen = {}

    def fake(path, **kwargs):
        seen["path"] = path
        seen["bytes"] = Path(path).read_bytes()
        seen["kwargs"] = kwargs
        return {"text": "  สวัสดีครับ  "}

    t = MlxWhisperTranscriber(model="m", transcribe_fn=fake)
    import asyncio

    text = asyncio.run(t.transcribe(b"abc", ".webm", "th"))
    assert text == "สวัสดีครับ"
    assert seen["path"].endswith(".webm")
    assert seen["bytes"] == b"abc"
    assert seen["kwargs"]["language"] == "th"
    assert seen["kwargs"]["path_or_hf_repo"] == "m"
    assert seen["kwargs"]["condition_on_previous_text"] is False
    assert not Path(seen["path"]).exists()


async def test_transcribe_is_async_and_returns_cleaned_text():
    t = MlxWhisperTranscriber(model="m", transcribe_fn=lambda path, **kw: {"text": "hello"})
    assert await t.transcribe(b"x", ".mp4", "en") == "hello"


def test_clean_transcript_drops_known_silence_hallucinations():
    assert clean_transcript("ขอบคุณที่รับชม") == ""
    assert clean_transcript(" Thank you for watching! ") == ""
    assert clean_transcript("วันนี้ไปทำงานครับ") == "วันนี้ไปทำงานครับ"


def test_silent_wav_is_valid_wav():
    with wave.open(io.BytesIO(silent_wav(0.5))) as w:
        assert w.getframerate() == 16000
        assert w.getnframes() == 8000


async def test_warm_up_calls_transcribe_with_wav():
    calls = []
    t = MlxWhisperTranscriber(model="m", transcribe_fn=lambda path, **kw: calls.append(path) or {"text": ""})
    await t.warm_up()
    assert len(calls) == 1 and calls[0].endswith(".wav")
