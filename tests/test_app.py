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
        return TestClient(create_app(manager, db)), db
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
        assert "audio_b64" in client.post(f"/api/sessions/{sid}/replay", json={"slow": True}).json()
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
