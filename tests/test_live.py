"""Live checks against the real services. Run: uv run pytest -m live -s"""

import os
import subprocess

import pytest

from pood_thai.brain import AgentSdkBrain
from pood_thai.models import Segment
from pood_thai.prompts import SESSION_START, build_system_prompt
from pood_thai.stt import MlxWhisperTranscriber
from pood_thai.topics import get_topic
from pood_thai.tts import AzureSpeaker, SaySpeaker
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
    audio = await SaySpeaker().synthesize([Segment("Tired is", "en"), Segment("เหนื่อยค่ะ", "th")], 0.9)
    assert audio.mime == "audio/wav" and len(audio.data) > 10_000


@pytest.mark.skipif(not os.environ.get("AZURE_SPEECH_KEY"), reason="AZURE_SPEECH_KEY not set")
async def test_azure_speaker_produces_mp3():
    speaker = AzureSpeaker(os.environ["AZURE_SPEECH_KEY"], os.environ.get("AZURE_SPEECH_REGION", "southeastasia"))
    audio = await speaker.synthesize([Segment("Tired is", "en"), Segment("เหนื่อยค่ะ", "th")], 0.9)
    assert audio.mime == "audio/mpeg" and len(audio.data) > 5_000


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
