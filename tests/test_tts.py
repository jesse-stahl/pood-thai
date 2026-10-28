import io
import wave
from pathlib import Path

import pytest

from pood_thai.models import DEFAULT_VOICE, Audio, Segment, SpeechError, Word
from pood_thai.tts import (
    VOICES,
    AzureSpeaker,
    Boundary,
    FallbackSpeaker,
    SaySpeaker,
    build_ssml,
    concat_wavs,
    map_words,
)


def _wav_bytes(frames: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


def _escaped_text_at(ssml, span):
    return ssml[span.start : span.start + len(span.index_map)]


def test_voice_catalog_has_the_default_and_a_multilingual_voice():
    assert DEFAULT_VOICE in VOICES
    assert VOICES["premwadee"].azure_name == "th-TH-PremwadeeNeural"
    assert VOICES["niwat"].azure_name == "th-TH-NiwatNeural"
    assert VOICES["ava-multi"].multilingual is True


def test_build_ssml_thai_voice_reads_english_with_the_english_voice():
    segs = [Segment("Tired is", "en", "note", 0), Segment("เหนื่อย", "th", "note", 9), Segment("เหนื่อยไหมคะ", "th")]
    ssml, spans = build_ssml(segs, "achara")
    assert ssml.startswith("<speak") and ssml.endswith("</speak>")
    assert ssml.count('<voice name="en-US-AvaNeural">') == 1
    assert ssml.count('<voice name="th-TH-AcharaNeural">') == 2
    assert "prosody" not in ssml
    assert [s.segment for s in spans] == segs
    for span in spans:
        assert _escaped_text_at(ssml, span) == span.segment.text


def test_build_ssml_multilingual_voice_wraps_thai_in_lang():
    segs = [Segment("Busy is", "en", "note", 0), Segment("ยุ่ง", "th", "note", 8)]
    ssml, spans = build_ssml(segs, "ava-multi")
    assert ssml.count("<voice ") == 1
    assert '<voice name="en-US-AvaMultilingualNeural">Busy is <lang xml:lang="th-TH">ยุ่ง</lang></voice>' in ssml
    for span in spans:
        assert _escaped_text_at(ssml, span) == span.segment.text


def test_build_ssml_escapes_markup_and_maps_back_to_raw_characters():
    seg = Segment("A & B <ค่ะ>", "en", "note", 0)
    ssml, spans = build_ssml([seg, Segment("  ", "th")], "premwadee")
    assert len(spans) == 1  # blank segments are not spoken
    escaped = _escaped_text_at(ssml, spans[0])
    assert escaped == "A &amp; B &lt;ค่ะ&gt;"
    assert spans[0].index_map[escaped.index("B")] == seg.text.index("B")


def test_map_words_uses_ssml_offsets():
    seg = Segment("โอ้ ฟังดู", "th")
    _, spans = build_ssml([seg], "premwadee")
    base = spans[0].start
    boundaries = [
        Boundary("Word", "โอ้", base + 0, 3, 0.05, 0.2),
        Boundary("Punctuation", " ", base + 3, 1, 0.25, 0.0),
        Boundary("Word", "ฟัง", base + 4, 3, 0.25, 0.225),
        Boundary("Word", "ดู", base + 7, 2, 0.475, 0.138),
    ]
    assert map_words(spans, boundaries) == (
        Word("thai", 0, 3, 0.05, 0.25),
        Word("thai", 4, 7, 0.25, 0.475),
        Word("thai", 7, 9, 0.475, 0.613),
    )


def test_map_words_adds_the_segment_offset_for_note_words():
    segs = [Segment("Tired is", "en", "note", 0), Segment("เหนื่อย", "th", "note", 9)]
    _, spans = build_ssml(segs, "premwadee")
    boundaries = [
        Boundary("Word", "Tired", spans[0].start, 5, 0.0, 0.3),
        Boundary("Word", "เหนื่อย", spans[1].start, 7, 0.5, 0.4),
    ]
    assert map_words(spans, boundaries) == (Word("note", 0, 5, 0.0, 0.3), Word("note", 9, 16, 0.5, 0.9))


def test_map_words_falls_back_to_text_search_when_offsets_do_not_match():
    segs = [Segment("Busy is", "en", "note", 0), Segment("ยุ่งมาก", "th")]
    _, spans = build_ssml(segs, "premwadee")
    boundaries = [
        Boundary("Word", "Busy", 9999, 4, 0.0, 0.2),
        Boundary("Word", "is", -1, 2, 0.2, 0.1),
        Boundary("Word", "ยุ่ง", 5, 4, 0.4, 0.3),
    ]
    assert map_words(spans, boundaries) == (
        Word("note", 0, 4, 0.0, 0.2),
        Word("note", 5, 7, 0.2, 0.3),
        Word("thai", 0, 4, 0.4, 0.7),
    )


def test_map_words_skips_words_it_cannot_place():
    _, spans = build_ssml([Segment("ค่ะ", "th")], "premwadee")
    assert map_words(spans, [Boundary("Word", "hello", -1, 5, 0.0, 0.1)]) == ()


async def test_azure_speaker_returns_wav_and_words_from_the_synth_function():
    seen = {}

    def fake_synth(ssml):
        seen["ssml"] = ssml
        start = ssml.index("สวัสดี")
        return b"RIFF", [Boundary("Word", "สวัสดี", start, 6, 0.1, 0.4), Boundary("Word", "ค่ะ", start + 6, 3, 0.5, 0.2)]

    audio = await AzureSpeaker("key", "southeastasia", synth_fn=fake_synth).synthesize(
        [Segment("สวัสดีค่ะ", "th")], "niwat"
    )
    assert audio.data == b"RIFF" and audio.mime == "audio/wav"
    assert 'name="th-TH-NiwatNeural"' in seen["ssml"]
    assert audio.words == (Word("thai", 0, 6, 0.1, 0.5), Word("thai", 6, 9, 0.5, 0.7))


async def test_azure_speaker_wraps_synth_failures():
    def boom(ssml):
        raise RuntimeError("network down")

    with pytest.raises(SpeechError, match="network down"):
        await AzureSpeaker("k", "r", synth_fn=boom).synthesize([Segment("ค่ะ", "th")], "premwadee")


async def test_azure_speaker_rejects_missing_key_and_unknown_voice():
    with pytest.raises(SpeechError, match="AZURE_SPEECH_KEY"):
        await AzureSpeaker("", "r", synth_fn=lambda s: (b"", [])).synthesize([Segment("ค่ะ", "th")], "premwadee")
    with pytest.raises(SpeechError, match="Unknown voice"):
        await AzureSpeaker("k", "r", synth_fn=lambda s: (b"", [])).synthesize([Segment("ค่ะ", "th")], "nope")


def test_concat_wavs_joins_frames(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(_wav_bytes(100))
    b.write_bytes(_wav_bytes(50))
    with wave.open(io.BytesIO(concat_wavs([a, b]))) as w:
        assert w.getnframes() == 150


async def test_say_speaker_runs_say_per_segment_and_has_no_word_timings():
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(_wav_bytes(10))
        assert Path(cmd[cmd.index("-f") + 1]).read_text(encoding="utf-8")

    audio = await SaySpeaker(run=fake_run).synthesize([Segment("Hello", "en"), Segment("สวัสดีค่ะ", "th")], "achara")
    assert audio.mime == "audio/wav" and audio.words == ()
    assert [c[c.index("-v") + 1] for c in commands] == ["Samantha", "Kanya"]
    assert "-r" not in commands[0]
    with wave.open(io.BytesIO(audio.data)) as w:
        assert w.getnframes() == 20


async def test_say_speaker_wraps_failures():
    def boom(cmd, **kwargs):
        raise OSError("no say")

    with pytest.raises(SpeechError):
        await SaySpeaker(run=boom).synthesize([Segment("ค่ะ", "th")], "premwadee")


class _Failing:
    async def synthesize(self, segments, voice):
        raise SpeechError("down")


class _Working:
    def __init__(self):
        self.voices = []

    async def synthesize(self, segments, voice):
        self.voices.append(voice)
        return Audio(b"WAV", "audio/wav")


async def test_fallback_speaker_uses_fallback_with_the_same_voice_and_flags_it():
    working = _Working()
    audio = await FallbackSpeaker(_Failing(), working).synthesize([Segment("ค่ะ", "th")], "achara")
    assert audio == Audio(b"WAV", "audio/wav", fallback_used=True)
    assert working.voices == ["achara"]


async def test_fallback_speaker_prefers_primary():
    audio = await FallbackSpeaker(_Working(), _Failing()).synthesize([Segment("ค่ะ", "th")], "premwadee")
    assert audio.fallback_used is False


async def test_azure_speaker_times_out_with_a_speech_error(monkeypatch):
    import time

    import pood_thai.tts as tts

    monkeypatch.setattr(tts, "AZURE_TIMEOUT_S", 0.05)

    def slow(ssml):
        time.sleep(0.3)
        return b"", []

    with pytest.raises(SpeechError, match="timed out"):
        await AzureSpeaker("k", "r", synth_fn=slow).synthesize([Segment("ค่ะ", "th")], "premwadee")


async def test_azure_runs_on_its_own_threads_so_a_hung_call_cannot_starve_the_fallback():
    import threading

    names = []

    def record(ssml):
        names.append(threading.current_thread().name)
        return b"RIFF", []

    await AzureSpeaker("k", "r", synth_fn=record).synthesize([Segment("ค่ะ", "th")], "premwadee")
    assert names[0].startswith("azure-tts")


def test_azure_sdk_is_told_to_give_up_on_stalled_audio(monkeypatch):
    import sys
    import types

    from pood_thai.tts import _azure_synthesize

    calls = []

    class Stop(Exception):
        pass

    class FakeConfig:
        def __init__(self, subscription, region):
            pass

        def set_speech_synthesis_output_format(self, fmt):
            pass

        def set_property(self, prop, value):
            calls.append((prop, value))

    class FakeSynthesizer:
        def __init__(self, speech_config, audio_config):
            raise Stop()  # enough: the config was built before this point

    fake_sdk = types.SimpleNamespace(
        SpeechConfig=FakeConfig,
        SpeechSynthesizer=FakeSynthesizer,
        SpeechSynthesisOutputFormat=types.SimpleNamespace(Riff24Khz16BitMonoPcm="wav"),
        PropertyId=types.SimpleNamespace(SpeechSynthesis_FrameTimeoutInterval="frame-timeout"),
    )
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices.speech", fake_sdk)
    with pytest.raises(Stop):
        _azure_synthesize("k", "r", "<speak/>")
    assert ("frame-timeout", "10000") in calls


def test_map_words_prefers_the_ssml_offset_over_the_first_text_match():
    seg = Segment("มา มา", "th")
    _, spans = build_ssml([seg], "premwadee")
    boundaries = [Boundary("Word", "มา", spans[0].start + 3, 2, 0.4, 0.2)]
    assert map_words(spans, boundaries) == (Word("thai", 3, 5, 0.4, 0.6),)
