import pytest

from pood_thai.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


def test_create_and_end_session(db):
    sid = db.create_session("day")
    assert sid == 1
    assert db.session_ended_at(sid) is None
    db.end_session(sid)
    assert db.session_ended_at(sid) is not None


def test_add_and_list_messages_in_order(db):
    sid = db.create_session("food")
    a = db.add_message(sid, "assistant", "th", thai="สวัสดีค่ะ", english="Hello")
    u = db.add_message(sid, "user", "th", thai="สวัสดีครับ", english="Hello")
    assert a.role == "assistant" and a.thai == "สวัสดีค่ะ" and a.english_note == ""
    assert [m.id for m in db.list_messages(sid)] == [a.id, u.id]


def test_messages_are_isolated_by_session(db):
    s1 = db.create_session("day")
    s2 = db.create_session("food")
    db.add_message(s1, "assistant", "th", thai="หนึ่ง")
    db.add_message(s2, "assistant", "th", thai="สอง")
    assert [m.thai for m in db.list_messages(s2)] == ["สอง"]


def test_delete_last_exchange_removes_last_user_message_and_replies_after_it(db):
    sid = db.create_session("day")
    opening = db.add_message(sid, "assistant", "th", thai="เปิด")
    u1 = db.add_message(sid, "user", "th", thai="หนึ่ง")
    a1 = db.add_message(sid, "assistant", "th", thai="ตอบหนึ่ง")
    u2 = db.add_message(sid, "user", "th", thai="สอง")
    a2 = db.add_message(sid, "assistant", "th", thai="ตอบสอง")
    removed = db.delete_last_exchange(sid)
    assert removed == [u2.id, a2.id]
    assert [m.id for m in db.list_messages(sid)] == [opening.id, u1.id, a1.id]


def test_delete_last_exchange_without_user_messages_returns_empty(db):
    sid = db.create_session("day")
    db.add_message(sid, "assistant", "th", thai="เปิด")
    assert db.delete_last_exchange(sid) == []
    assert len(db.list_messages(sid)) == 1


def test_replace_facts_keeps_rows_with_identical_text(db):
    first = db.replace_facts(["Works as a developer.", "Has a cat."], source_session_id=None)
    sid = db.create_session("day")
    second = db.replace_facts(["Works as a developer.", "Visited Chiang Mai."], source_session_id=sid)
    assert [f.fact for f in second] == ["Works as a developer.", "Visited Chiang Mai."]
    kept = next(f for f in second if f.fact == "Works as a developer.")
    original = next(f for f in first if f.fact == "Works as a developer.")
    assert kept.id == original.id
    assert next(f for f in second if f.fact == "Visited Chiang Mai.").source_session_id == sid
    assert [f.fact for f in db.list_facts()] == ["Works as a developer.", "Visited Chiang Mai."]


def test_delete_fact(db):
    facts = db.replace_facts(["A.", "B."], source_session_id=None)
    assert db.delete_fact(facts[0].id) is True
    assert db.delete_fact(facts[0].id) is False
    assert [f.fact for f in db.list_facts()] == ["B."]
