import pytest

from pood_thai.conversation import (
    DEFAULT_RATE,
    SLOW_RATE,
    ConversationManager,
    NoSpeech,
    SessionNotFound,
)
from pood_thai.db import Database
from pood_thai.models import BrainError, Segment
from pood_thai.prompts import MEMORY_SYSTEM_PROMPT, SESSION_START, UNDO_NOTE
from tests.fakes import FakeBrain, FakeSpeaker, FakeTranscriber, reply_json

OPENING = reply_json(thai="สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", english="Hi! How was today?")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    yield database
    database.close()


def make(db, replies, texts=(), completion='{"facts": []}'):
    brain = FakeBrain([replies], completion=completion)
    stt = FakeTranscriber(list(texts))
    speaker = FakeSpeaker()
    manager = ConversationManager(db, brain, stt, speaker, today=lambda: "2026-10-26")
    return manager, brain, stt, speaker


async def test_start_opens_chat_with_topic_prompt_and_speaks_greeting(db):
    manager, brain, _, speaker = make(db, [OPENING])
    result = await manager.start("food")
    assert "อาหาร" in brain.system_prompts[0]
    assert brain.chats[0].sent == [SESSION_START]
    assert result.message.role == "assistant"
    assert result.message.thai == "สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ"
    assert speaker.calls == [([Segment("สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", "th")], DEFAULT_RATE)]
    assert result.audio.data.startswith(b"AUDIO:")
    assert [m.id for m in db.list_messages(result.session_id)] == [result.message.id]


async def test_start_includes_memory_facts_in_prompt(db):
    db.replace_facts(["Has a cat named Mochi."], source_session_id=None)
    manager, brain, _, _ = make(db, [OPENING])
    await manager.start("day")
    assert "- Has a cat named Mochi." in brain.system_prompts[0]


async def test_start_unknown_topic_raises_value_error(db):
    manager, _, _, _ = make(db, [OPENING])
    with pytest.raises(ValueError):
        await manager.start("nope")


async def test_start_closes_chat_when_opening_reply_is_unusable(db):
    manager, brain, _, _ = make(db, [""])
    with pytest.raises(BrainError):
        await manager.start("day")
    assert brain.chats[0].closed


async def test_thai_turn_stores_both_messages_and_speaks_reply(db):
    reply = reply_json(thai="เหนื่อยเหรอคะ", english="Tired?", user_english="I went to work, very tired")
    manager, brain, stt, speaker = make(db, [OPENING, reply], texts=["วันนี้ไปทำงาน เหนื่อยมาก"])
    sid = (await manager.start("day")).session_id
    result = await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert stt.calls == [(b"rec", ".webm", "th")]
    assert brain.chats[0].sent[-1] == "[Thai] วันนี้ไปทำงาน เหนื่อยมาก"
    assert result.kind == "reply"
    assert result.user_message.thai == "วันนี้ไปทำงาน เหนื่อยมาก"
    assert result.user_message.english == "I went to work, very tired"
    assert result.assistant_message.thai == "เหนื่อยเหรอคะ"
    assert speaker.calls[-1] == ([Segment("เหนื่อยเหรอคะ", "th")], DEFAULT_RATE)
    assert len(db.list_messages(sid)) == 3


async def test_english_turn_speaks_note_in_english_then_thai(db):
    reply = reply_json(thai="วันนี้เหนื่อยไหมคะ", english="Are you tired today?", english_note="Tired is เหนื่อย.")
    manager, brain, _, speaker = make(db, [OPENING, reply], texts=["how do I say tired"])
    sid = (await manager.start("day")).session_id
    result = await manager.handle_audio(sid, b"rec", ".webm", "en")
    assert brain.chats[0].sent[-1] == "[English question] how do I say tired"
    assert result.user_message.lang == "en"
    assert result.user_message.thai == ""
    assert result.user_message.english == "how do I say tired"
    assert speaker.calls[-1][0] == [Segment("Tired is เหนื่อย.", "en"), Segment("วันนี้เหนื่อยไหมคะ", "th")]


async def test_empty_transcript_raises_no_speech_without_calling_claude(db):
    manager, brain, _, _ = make(db, [OPENING], texts=[""])
    sid = (await manager.start("day")).session_id
    with pytest.raises(NoSpeech):
        await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert brain.chats[0].sent == [SESSION_START]


async def test_brain_failure_stores_nothing(db):
    manager, _, _, _ = make(db, [OPENING, ""], texts=["สวัสดี"])
    sid = (await manager.start("day")).session_id
    with pytest.raises(BrainError):
        await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert len(db.list_messages(sid)) == 1


async def test_repeat_slower_sets_slow_rate_and_respeaks_last_reply(db):
    manager, _, _, speaker = make(
        db, [OPENING, reply_json(action="repeat_slower", thai="", english=""), reply_json(action="repeat", thai="", english="")],
        texts=["ช้าๆ หน่อย", "อีกครั้ง"],
    )
    sid = (await manager.start("day")).session_id
    slow = await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert slow.kind == "repeat" and slow.user_message is None and slow.assistant_message is None
    assert speaker.calls[-1] == ([Segment("สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", "th")], SLOW_RATE)
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert speaker.calls[-1][1] == SLOW_RATE
    assert len(db.list_messages(sid)) == 1


async def test_replay_slow_switches_rate(db):
    manager, _, _, speaker = make(db, [OPENING])
    sid = (await manager.start("day")).session_id
    await manager.replay(sid, slow=False)
    assert speaker.calls[-1][1] == DEFAULT_RATE
    await manager.replay(sid, slow=True)
    assert speaker.calls[-1][1] == SLOW_RATE


async def test_undo_removes_last_exchange_and_flags_next_message(db):
    manager, brain, _, speaker = make(
        db, [OPENING, reply_json(thai="ตอบผิด"), reply_json(thai="ตอบใหม่")], texts=["ผิด", "ถูก"]
    )
    sid = (await manager.start("day")).session_id
    first = await manager.handle_audio(sid, b"rec", ".webm", "th")
    removed = await manager.undo(sid)
    assert removed == [first.user_message.id, first.assistant_message.id]
    await manager.replay(sid, slow=False)
    assert speaker.calls[-1][0] == [Segment("สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", "th")]
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert brain.chats[0].sent[-1] == f"{UNDO_NOTE}\n[Thai] ถูก"


async def test_undo_with_nothing_to_undo_returns_empty_and_sets_no_note(db):
    manager, brain, _, _ = make(db, [OPENING, reply_json()], texts=["สวัสดี"])
    sid = (await manager.start("day")).session_id
    assert await manager.undo(sid) == []
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert brain.chats[0].sent[-1] == "[Thai] สวัสดี"


async def test_end_closes_chat_marks_ended_and_replaces_facts(db):
    manager, brain, _, _ = make(
        db, [OPENING, reply_json(user_english="I have a cat")], texts=["ฉันมีแมว"],
        completion='{"facts": ["Has a cat."]}',
    )
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    facts = await manager.end(sid)
    assert brain.chats[0].closed
    assert db.session_ended_at(sid) is not None
    assert [f.fact for f in facts] == ["Has a cat."]
    system, request = brain.complete_calls[0]
    assert system == MEMORY_SYSTEM_PROMPT
    assert "Today's date: 2026-10-26" in request and "ฉันมีแมว" in request
    with pytest.raises(SessionNotFound):
        await manager.replay(sid, slow=False)


async def test_end_without_user_turns_skips_memory_extraction(db):
    db.replace_facts(["Old fact."], source_session_id=None)
    manager, brain, _, _ = make(db, [OPENING])
    sid = (await manager.start("day")).session_id
    facts = await manager.end(sid)
    assert brain.complete_calls == []
    assert [f.fact for f in facts] == ["Old fact."]


async def test_end_keeps_facts_when_memory_reply_is_garbage(db):
    db.replace_facts(["Old fact."], source_session_id=None)
    manager, _, _, _ = make(db, [OPENING, reply_json()], texts=["สวัสดี"], completion="sorry, no")
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert [f.fact for f in await manager.end(sid)] == ["Old fact."]


async def test_unknown_session_raises(db):
    manager, _, _, _ = make(db, [OPENING])
    with pytest.raises(SessionNotFound):
        await manager.handle_audio(999, b"rec", ".webm", "th")
    with pytest.raises(SessionNotFound):
        await manager.undo(999)
    with pytest.raises(SessionNotFound):
        await manager.end(999)


async def test_starting_new_session_closes_previous_chat(db):
    brain = FakeBrain([[OPENING], [OPENING]])
    manager = ConversationManager(db, brain, FakeTranscriber([]), FakeSpeaker(), today=lambda: "2026-10-26")
    first = await manager.start("day")
    await manager.start("food")
    assert brain.chats[0].closed
    assert db.session_ended_at(first.session_id) is not None
    with pytest.raises(SessionNotFound):
        await manager.replay(first.session_id, slow=False)


async def test_close_all_closes_live_chats(db):
    manager, brain, _, _ = make(db, [OPENING])
    await manager.start("day")
    await manager.close_all()
    assert brain.chats[0].closed


async def test_two_undos_tell_claude_both_messages_were_withdrawn(db):
    from pood_thai.prompts import undo_note

    manager, brain, _, _ = make(
        db, [OPENING, reply_json(thai="หนึ่ง"), reply_json(thai="สอง"), reply_json(thai="สาม")], texts=["ก", "ข", "ค"]
    )
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert len(await manager.undo(sid)) == 2
    assert len(await manager.undo(sid)) == 2
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert brain.chats[0].sent[-1] == f"{undo_note(2)}\n[Thai] ค"


async def test_undo_after_repeat_withdraws_the_repeat_request_too(db):
    from pood_thai.prompts import undo_note

    manager, brain, _, _ = make(
        db,
        [OPENING, reply_json(thai="ตอบ"), reply_json(action="repeat", thai="", english=""), reply_json(thai="ใหม่")],
        texts=["ผิด", "อีกครั้ง", "ถูก"],
    )
    sid = (await manager.start("day")).session_id
    first = await manager.handle_audio(sid, b"rec", ".webm", "th")
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert await manager.undo(sid) == [first.user_message.id, first.assistant_message.id]
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    # Claude saw "ผิด" and "อีกครั้ง" after the last kept exchange, so both must be withdrawn.
    assert brain.chats[0].sent[-1] == f"{undo_note(2)}\n[Thai] ถูก"


async def test_overlapping_starts_leave_only_one_live_session(db):
    import asyncio

    class SlowBrain(FakeBrain):
        async def open_chat(self, system_prompt):
            await asyncio.sleep(0.01)
            return await super().open_chat(system_prompt)

    brain = SlowBrain([[OPENING], [OPENING]])
    manager = ConversationManager(db, brain, FakeTranscriber([]), FakeSpeaker(), today=lambda: "2026-10-26")
    first, second = await asyncio.gather(manager.start("day"), manager.start("food"))
    assert brain.chats[0].closed and not brain.chats[1].closed
    with pytest.raises(SessionNotFound):
        await manager.replay(first.session_id, slow=False)
    await manager.replay(second.session_id, slow=False)


async def test_undo_after_a_failed_turn_withdraws_the_message_claude_saw(db):
    from pood_thai.prompts import undo_note

    manager, brain, _, _ = make(
        db, [OPENING, reply_json(thai="ตอบ"), "{garbled", reply_json(thai="ใหม่")], texts=["ผิด", "เอ็กซ์", "ถูก"]
    )
    sid = (await manager.start("day")).session_id
    first = await manager.handle_audio(sid, b"rec", ".webm", "th")
    with pytest.raises(BrainError):
        await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert await manager.undo(sid) == [first.user_message.id, first.assistant_message.id]
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert brain.chats[0].sent[-1] == f"{undo_note(2)}\n[Thai] ถูก"


async def test_end_keeps_existing_facts_when_memory_reply_is_empty(db):
    db.replace_facts(["Has a cat named Mochi."], source_session_id=None)
    manager, _, _, _ = make(db, [OPENING, reply_json()], texts=["สวัสดี"], completion='{"facts": []}')
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert [f.fact for f in await manager.end(sid)] == ["Has a cat named Mochi."]
