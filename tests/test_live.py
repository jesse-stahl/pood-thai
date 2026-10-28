"""Live checks against the real services. Run: uv run pytest -m live -s"""

import os
import subprocess

import pytest

from pood_thai.brain import AgentSdkBrain
from pood_thai.models import Segment
from pood_thai.prompts import SESSION_START, build_system_prompt
from pood_thai.stt import MlxWhisperTranscriber
from pood_thai.topics import get_topic
from pood_thai.tts import VOICES, AzureSpeaker, SaySpeaker
from pood_thai.turns import parse_turn

pytestmark = pytest.mark.live


async def test_whisper_transcribes_thai_speech(tmp_path):
    wav = tmp_path / "hello.wav"
    subprocess.run(
        ["say", "-v", "Kanya", "--file-format=WAVE", "--data-format=LEI16@16000", "-o", str(wav), "สวัสดีค่ะ วันนี้อากาศดีมาก"],
        check=True,
    )
    text = await MlxWhisperTranscriber().transcribe(wav.read_bytes(), ".wav", "th")
    print("whisper:", text)
    assert "สวัสดี" in text


async def test_say_speaker_produces_wav():
    audio = await SaySpeaker().synthesize([Segment("Tired is", "en"), Segment("เหนื่อยค่ะ", "th")], "premwadee")
    assert audio.mime == "audio/wav" and len(audio.data) > 10_000


@pytest.mark.skipif(not os.environ.get("AZURE_SPEECH_KEY"), reason="AZURE_SPEECH_KEY not set")
async def test_azure_voices_return_wav_with_thai_word_timings():
    speaker = AzureSpeaker(os.environ["AZURE_SPEECH_KEY"], os.environ.get("AZURE_SPEECH_REGION", "southeastasia"))
    segments = [
        Segment("Tired is", "en", "note", 0),
        Segment("เหนื่อย", "th", "note", 9),
        Segment("วันนี้งานเยอะมากเหรอคะ", "th"),
    ]
    for voice in VOICES:
        audio = await speaker.synthesize(segments, voice)
        print(voice, len(audio.data), audio.words)
        thai_words = [w for w in audio.words if w.part == "thai"]
        assert audio.mime == "audio/wav" and len(audio.data) > 20_000
        assert len(thai_words) >= 3, f"{voice}: no Thai word timings"
        assert all(a.t_start <= b.t_start for a, b in zip(audio.words, audio.words[1:]))


async def test_claude_opening_turn_is_valid_thai_json(tmp_path):
    brain = AgentSdkBrain("sonnet", tmp_path)
    chat = await brain.open_chat(build_system_prompt(get_topic("day"), []))
    try:
        turn = parse_turn(await chat.send(SESSION_START))
        print("claude:", turn)
        assert turn.action == "reply" and turn.thai and turn.english
        assert any("฀" <= ch <= "๿" for ch in turn.thai)
        reply = parse_turn(await chat.send("[Thai] ช้าๆ หน่อย"))
        assert reply.action == "repeat_slower"
    finally:
        await chat.close()
