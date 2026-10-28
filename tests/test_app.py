import base64

import pytest
from fastapi.testclient import TestClient

from pood_thai.app import create_app
from pood_thai.conversation import ConversationManager
from pood_thai.db import Database
from tests.fakes import FakeBrain, FakeSpeaker, FakeTranscriber, reply_json

OPENING = reply_json(thai="สวัสดีค่ะ", english="Hello")


@pytest.fixture
def make_client(tmp_path):
    def make(replies, texts=(), completion='{"facts": []}'):
        db = Database(tmp_path / "t.sqlite3")
        manager = ConversationManager(
            db, FakeBrain([replies], completion=completion), FakeTranscriber(list(texts)), FakeSpeaker(),
            today=lambda: "2026-10-26",
        )
        return TestClient(create_app(manager, db), base_url="http://127.0.0.1:8765"), db
    return make


def _start(client, topic="day"):
    r = client.post("/api/sessions", json={"topic": topic})
    assert r.status_code == 200, r.text
    return r.json()


def test_health_topics_and_index(make_client):
    client, _ = make_client([OPENING])
    with client:
        assert client.get("/api/health").json() == {"ok": True}
        topics = client.get("/api/topics").json()["topics"]
        assert topics[0] == {"id": "day", "label": "Your day", "thai": "วันนี้ของคุณ"}
        index = client.get("/")
        assert index.status_code == 200 and "text/html" in index.headers["content-type"]


def test_start_session_returns_message_and_audio(make_client):
    client, _ = make_client([OPENING])
    with client:
        body = _start(client)
        assert body["session_id"] == 1
        assert body["message"] == {"id": 1, "role": "assistant", "lang": "th", "thai": "สวัสดีค่ะ", "english": "Hello", "english_note": ""}
        assert base64.b64decode(body["audio_b64"]) == "AUDIO:สวัสดีค่ะ".encode()
        assert body["audio_mime"] == "audio/mpeg"
        assert body["words"] == [{"part": "thai", "start": 0, "end": 9, "t_start": 0.0, "t_end": 0.5}]
        assert body["fallback_voice"] is False


def test_start_unknown_topic_is_400(make_client):
    client, _ = make_client([OPENING])
    with client:
        r = client.post("/api/sessions", json={"topic": "nope"})
        assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_turn_reply(make_client):
    client, _ = make_client([OPENING, reply_json(thai="ดีค่ะ", english="Good", user_english="I'm fine")], texts=["สบายดี"])
    with client:
        sid = _start(client)["session_id"]
        r = client.post(f"/api/sessions/{sid}/turns", files={"audio": ("recording.webm", b"x", "audio/webm")}, data={"lang": "th"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "reply"
        assert body["user_message"]["thai"] == "สบายดี" and body["user_message"]["english"] == "I'm fine"
        assert body["assistant_message"]["thai"] == "ดีค่ะ"


def test_turn_repeat_has_null_messages(make_client):
    client, _ = make_client([OPENING, reply_json(action="repeat", thai="", english="")], texts=["อีกครั้ง"])
    with client:
        sid = _start(client)["session_id"]
        body = client.post(f"/api/sessions/{sid}/turns", files={"audio": ("r.mp4", b"x", "audio/mp4")}, data={"lang": "th"}).json()
        assert body["kind"] == "repeat" and body["user_message"] is None and body["assistant_message"] is None
        assert body["replay_message_id"] == 1 and body["audio_b64"] is None and body["words"] == []


def test_turn_errors(make_client):
    client, _ = make_client([OPENING, ""], texts=["", "สวัสดี"])
    with client:
        sid = _start(client)["session_id"]
        files = {"audio": ("r.webm", b"x", "audio/webm")}
        assert client.post(f"/api/sessions/{sid}/turns", files=files, data={"lang": "fr"}).status_code == 400
        r = client.post(f"/api/sessions/{sid}/turns", files=files, data={"lang": "th"})
        assert r.status_code == 422 and r.json()["error"] == "no_speech"
        r = client.post(f"/api/sessions/{sid}/turns", files=files, data={"lang": "th"})
        assert r.status_code == 503 and r.json()["error"] == "brain_unavailable"
        r = client.post("/api/sessions/999/turns", files=files, data={"lang": "th"})
        assert r.status_code == 404 and r.json()["error"] == "session_not_found"


def test_replay_undo_end_and_memory(make_client):
    client, db = make_client(
        [OPENING, reply_json(thai="ค่ะ", user_english="I have a cat")], texts=["มีแมว"],
        completion='{"facts": ["Has a cat."]}',
    )
    with client:
        sid = _start(client)["session_id"]
        speech = client.post(f"/api/sessions/{sid}/messages/1/speech", json={"voice": "achara"}).json()
        assert speech["audio_b64"] and speech["words"][0]["part"] == "thai"
        turn = client.post(f"/api/sessions/{sid}/turns", files={"audio": ("r.webm", b"x", "audio/webm")}, data={"lang": "th"}).json()
        assert client.post(f"/api/sessions/{sid}/undo").json() == {"removed_ids": [turn["user_message"]["id"], turn["assistant_message"]["id"]]}
        db.add_message(sid, "user", "th", thai="มีแมว", english="I have a cat")
        facts = client.post(f"/api/sessions/{sid}/end").json()["facts"]
        assert [f["fact"] for f in facts] == ["Has a cat."]
        listed = client.get("/api/memory").json()["facts"]
        assert listed == [{"id": facts[0]["id"], "fact": "Has a cat."}]
        assert client.delete(f"/api/memory/{facts[0]['id']}").json() == {"ok": True}
        r = client.delete(f"/api/memory/{facts[0]['id']}")
        assert r.status_code == 404 and r.json()["error"] == "fact_not_found"


def test_requests_from_other_hosts_or_origins_are_rejected(make_client):
    client, _ = make_client([OPENING])
    with client:
        assert client.get("/api/memory", headers={"host": "evil.example:8765"}).status_code == 403
        r = client.post("/api/sessions", json={"topic": "day"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
        assert client.get("/api/memory", headers={"host": "localhost:8765"}).status_code == 200
        assert client.post("/api/sessions", json={"topic": "day"}, headers={"origin": "http://127.0.0.1:8765"}).status_code == 200


def test_build_speaker_uses_mac_voice_directly_without_azure_key():
    from pood_thai.app import build_speaker
    from pood_thai.config import Settings
    from pood_thai.tts import FallbackSpeaker, SaySpeaker

    assert isinstance(build_speaker(Settings.from_env({})), SaySpeaker)
    assert isinstance(build_speaker(Settings.from_env({"AZURE_SPEECH_KEY": "k"})), FallbackSpeaker)


async def test_safe_warm_up_logs_instead_of_raising(caplog):
    from pood_thai.app import safe_warm_up
    from pood_thai.models import TranscriptionError

    class Broken:
        async def warm_up(self):
            raise TranscriptionError("could not read the recording")

    await safe_warm_up(Broken())
    assert "ffmpeg" in caplog.text


def test_voices_endpoint_without_azure_is_empty(make_client):
    client, _ = make_client([OPENING])
    with client:
        assert client.get("/api/voices").json() == {"voices": [], "default": "premwadee"}


def test_voice_catalog_validates_and_passes_voices_through(tmp_path):
    from pood_thai.tts import VOICES

    db = Database(tmp_path / "v.sqlite3")
    speaker = FakeSpeaker()
    manager = ConversationManager(
        db, FakeBrain([[OPENING, reply_json(thai="ดีค่ะ")]]), FakeTranscriber(["สบายดี"]), speaker,
        today=lambda: "2026-10-26",
    )
    client = TestClient(create_app(manager, db, voices=list(VOICES.values())), base_url="http://127.0.0.1:8765")
    with client:
        body = client.get("/api/voices").json()
        assert body["default"] == "premwadee"
        assert {"id": "achara", "label": VOICES["achara"].label} in body["voices"]
        r = client.post("/api/sessions", json={"topic": "day", "voice": "nope"})
        assert r.status_code == 400 and r.json()["error"] == "bad_request"
        sid = client.post("/api/sessions", json={"topic": "day", "voice": "niwat"}).json()["session_id"]
        assert speaker.calls[-1][1] == "niwat"
        files = {"audio": ("r.webm", b"x", "audio/webm")}
        r = client.post(f"/api/sessions/{sid}/turns", files=files, data={"lang": "th", "voice": "nope"})
        assert r.status_code == 400
        r = client.post(f"/api/sessions/{sid}/turns", files=files, data={"lang": "th", "voice": "achara"})
        assert r.status_code == 200 and speaker.calls[-1][1] == "achara"
        r = client.post(f"/api/sessions/{sid}/messages/999/speech", json={"voice": "achara"})
        assert r.status_code == 404 and r.json()["error"] == "message_not_found"
        r = client.post(f"/api/sessions/{sid}/messages/1/speech", json={"voice": "ava-multi"})
        assert r.status_code == 200 and speaker.calls[-1][1] == "ava-multi"
