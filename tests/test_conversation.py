import pytest

from pood_thai.conversation import (
    ConversationManager,
    MessageNotFound,
    NoSpeech,
    ReviewNotFound,
    SessionNotFound,
)
from pood_thai.db import Database
from pood_thai.models import DEFAULT_VOICE, BrainError, ReviewItem, Segment
from pood_thai.prompts import MEMORY_SYSTEM_PROMPT, REVIEW_SYSTEM_PROMPT, SESSION_START, UNDO_NOTE
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
    assert speaker.calls == [([Segment("สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", "th")], DEFAULT_VOICE)]
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
    assert speaker.calls[-1] == ([Segment("เหนื่อยเหรอคะ", "th")], DEFAULT_VOICE)
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
    assert speaker.calls[-1][0] == [Segment("Tired is", "en", "note", 0), Segment("เหนื่อย", "th", "note", 9), Segment("วันนี้เหนื่อยไหมคะ", "th")]


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


async def test_repeat_commands_store_nothing_and_point_at_the_last_reply(db):
    manager, _, _, speaker = make(
        db,
        [OPENING, reply_json(action="repeat_slower", thai="", english=""), reply_json(action="repeat", thai="", english="")],
        texts=["ช้าๆ หน่อย", "อีกครั้ง"],
    )
    start = await manager.start("day")
    calls_before = len(speaker.calls)
    for _ in range(2):
        result = await manager.handle_audio(start.session_id, b"rec", ".webm", "th")
        assert result.kind == "repeat" and result.user_message is None and result.assistant_message is None
        assert result.audio is None and result.replay_message_id == start.message.id
    assert len(speaker.calls) == calls_before  # the browser replays its cached audio
    assert len(db.list_messages(start.session_id)) == 1


async def test_speak_rerenders_a_reply_in_another_voice(db):
    manager, _, _, speaker = make(db, [OPENING])
    start = await manager.start("day", voice="achara")
    assert speaker.calls[-1][1] == "achara"
    audio = await manager.speak(start.session_id, start.message.id, "niwat")
    assert speaker.calls[-1] == ([Segment("สวัสดีค่ะ วันนี้เป็นยังไงบ้างคะ", "th")], "niwat")
    assert audio.words[0].part == "thai"
    with pytest.raises(MessageNotFound):
        await manager.speak(start.session_id, 999, "niwat")


async def test_turn_uses_the_requested_voice(db):
    manager, _, _, speaker = make(db, [OPENING, reply_json(thai="ดีค่ะ")], texts=["สบายดี"])
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th", voice="ava-multi")
    assert speaker.calls[-1][1] == "ava-multi"


async def test_repeat_after_undo_points_at_the_earlier_reply(db):
    manager, _, _, _ = make(
        db, [OPENING, reply_json(thai="ตอบ"), reply_json(action="repeat", thai="", english="")], texts=["ผิด", "อีกครั้ง"]
    )
    start = await manager.start("day")
    await manager.handle_audio(start.session_id, b"rec", ".webm", "th")
    await manager.undo(start.session_id)
    result = await manager.handle_audio(start.session_id, b"rec", ".webm", "th")
    assert result.replay_message_id == start.message.id


async def test_undo_removes_last_exchange_and_flags_next_message(db):
    manager, brain, _, speaker = make(
        db, [OPENING, reply_json(thai="ตอบผิด"), reply_json(thai="ตอบใหม่")], texts=["ผิด", "ถูก"]
    )
    sid = (await manager.start("day")).session_id
    first = await manager.handle_audio(sid, b"rec", ".webm", "th")
    removed = await manager.undo(sid)
    assert removed == [first.user_message.id, first.assistant_message.id]
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
    facts = (await manager.end(sid)).facts
    assert brain.chats[0].closed
    assert db.session_ended_at(sid) is not None
    assert [f.fact for f in facts] == ["Has a cat."]
    system, request = next(c for c in brain.complete_calls if c[0] == MEMORY_SYSTEM_PROMPT)
    assert system == MEMORY_SYSTEM_PROMPT
    assert "Today's date: 2026-10-26" in request and "ฉันมีแมว" in request
    with pytest.raises(SessionNotFound):
        await manager.speak(sid, 1, DEFAULT_VOICE)


async def test_end_without_user_turns_skips_memory_extraction(db):
    db.replace_facts(["Old fact."], source_session_id=None)
    manager, brain, _, _ = make(db, [OPENING])
    sid = (await manager.start("day")).session_id
    facts = (await manager.end(sid)).facts
    assert brain.complete_calls == []
    assert [f.fact for f in facts] == ["Old fact."]


async def test_end_keeps_facts_when_memory_reply_is_garbage(db):
    db.replace_facts(["Old fact."], source_session_id=None)
    manager, _, _, _ = make(db, [OPENING, reply_json()], texts=["สวัสดี"], completion="sorry, no")
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    assert [f.fact for f in (await manager.end(sid)).facts] == ["Old fact."]


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
        await manager.speak(first.session_id, first.message.id, DEFAULT_VOICE)


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
        await manager.speak(first.session_id, first.message.id, DEFAULT_VOICE)
    await manager.speak(second.session_id, second.message.id, DEFAULT_VOICE)


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
    assert [f.fact for f in (await manager.end(sid)).facts] == ["Has a cat named Mochi."]


def test_speech_segments_speak_thai_inside_english_notes_with_the_thai_voice():
    from pood_thai.conversation import speech_segments
    from pood_thai.models import Message

    note = "You say อร่อย มาก for very tasty. Try it!"
    message = Message(1, 1, "assistant", "th", "ลองพูดดูนะคะ", "Try saying it", note, "t")
    assert speech_segments(message) == [
        Segment("You say", "en", "note", 0),
        Segment("อร่อย มาก", "th", "note", 8),
        Segment("for very tasty. Try it!", "en", "note", 18),
        Segment("ลองพูดดูนะคะ", "th"),
    ]


REVIEW = (
    '{"items": [{"n": 1, "ok": false, "better": "ฉันมีแมวหนึ่งตัว", "why": "Add the number and classifier."}],'
    ' "tips": ["Use classifiers when you count."]}'
)


def memory_and_review(memory_json, review_json=REVIEW):
    def complete(system_prompt, message):
        return review_json if system_prompt == REVIEW_SYSTEM_PROMPT else memory_json
    return complete


async def test_end_reviews_the_thai_turns_and_saves_the_review(db):
    manager, brain, _, _ = make(
        db, [OPENING, reply_json(user_english="I have a cat")], texts=["ฉันมีแมว"],
        completion=memory_and_review('{"facts": ["Has a cat."]}'),
    )
    sid = (await manager.start("food")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    result = await manager.end(sid)
    assert [f.fact for f in result.facts] == ["Has a cat."]
    review = result.review
    assert review.session_id == sid and review.topic == "food"
    assert review.items == (ReviewItem("ฉันมีแมว", False, "ฉันมีแมวหนึ่งตัว", "Add the number and classifier."),)
    assert review.tips == ("Use classifiers when you count.",)
    assert db.get_review(review.id) == review
    review_call = next(c for c in brain.complete_calls if c[0] == REVIEW_SYSTEM_PROMPT)
    assert "[1] Learner: ฉันมีแมว" in review_call[1]


async def test_end_without_thai_turns_makes_no_review(db):
    manager, brain, _, _ = make(
        db, [OPENING, reply_json(english_note="Cat is แมว.")], texts=["how do I say cat"],
        completion=memory_and_review('{"facts": []}'),
    )
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "en")
    result = await manager.end(sid)
    assert result.review is None
    assert all(c[0] != REVIEW_SYSTEM_PROMPT for c in brain.complete_calls)


async def test_a_failed_review_still_saves_memory(db):
    manager, _, _, _ = make(
        db, [OPENING, reply_json()], texts=["ฉันมีแมว"],
        completion=memory_and_review('{"facts": ["Has a cat."]}', review_json="sorry, no"),
    )
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    result = await manager.end(sid)
    assert result.review is None
    assert [f.fact for f in result.facts] == ["Has a cat."]
    assert db.list_reviews() == []


async def test_speak_review_speaks_the_better_version_only(db):
    manager, _, _, speaker = make(
        db, [OPENING, reply_json()], texts=["ฉันมีแมว"], completion=memory_and_review('{"facts": []}'),
    )
    sid = (await manager.start("day")).session_id
    await manager.handle_audio(sid, b"rec", ".webm", "th")
    review = (await manager.end(sid)).review
    audio = await manager.speak_review(review.id, 0, "achara")
    assert speaker.calls[-1] == ([Segment("ฉันมีแมวหนึ่งตัว", "th")], "achara")
    assert audio.words[0].part == "thai"
    ok_review = db.add_review(sid, "day", (ReviewItem("ดีครับ", True),), ())
    for review_id, index in ((review.id, 5), (999, 0), (ok_review.id, 0)):
        with pytest.raises(ReviewNotFound):
            await manager.speak_review(review_id, index, "achara")
