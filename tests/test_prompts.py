import pytest

from pood_thai.models import Message
from pood_thai.prompts import (
    MEMORY_SYSTEM_PROMPT,
    SESSION_START,
    UNDO_NOTE,
    build_memory_request,
    build_system_prompt,
    format_user_message,
)
from pood_thai.topics import TOPICS, get_topic


def _msg(role, thai="", english="", english_note="", lang="th"):
    return Message(1, 1, role, lang, thai, english, english_note, "t")


def test_topics_have_unique_ids_and_free_chat():
    ids = [t.id for t in TOPICS]
    assert len(ids) == len(set(ids))
    assert ids[0] == "day" and "free" in ids


def test_get_topic_unknown_raises_value_error():
    with pytest.raises(ValueError):
        get_topic("nope")


def test_system_prompt_contains_topic_facts_and_json_contract():
    prompt = build_system_prompt(get_topic("food"), ["Has a cat named Mochi."])
    assert "Food" in prompt and "อาหาร" in prompt
    assert "- Has a cat named Mochi." in prompt
    assert '"action"' in prompt and '"user_english"' in prompt and '"english_note"' in prompt
    assert "ค่ะ" in prompt


def test_system_prompt_without_facts_says_nothing_yet():
    assert "- Nothing yet." in build_system_prompt(get_topic("day"), [])


def test_format_user_message_tags_language():
    assert format_user_message("สวัสดี", "th") == "[Thai] สวัสดี"
    assert format_user_message("how do I say tired", "en") == "[English question] how do I say tired"


def test_format_user_message_prepends_undo_note():
    assert format_user_message("สวัสดี", "th", withdrawn=True) == f"{UNDO_NOTE}\n[Thai] สวัสดี"


def test_session_start_is_tagged():
    assert SESSION_START.startswith("[Session start]")


def test_memory_request_includes_date_facts_and_transcript():
    messages = [
        _msg("assistant", thai="วันนี้เป็นยังไงบ้างคะ", english="How was today?"),
        _msg("user", thai="ไปหาหมอ", english="I went to the doctor"),
        _msg("user", english="how do I say tired", lang="en"),
        _msg("assistant", thai="เหนื่อย", english="tired", english_note="Tired is เหนื่อย."),
    ]
    text = build_memory_request(messages, ["Works as a developer."], "2026-10-26")
    assert "Today's date: 2026-10-26" in text
    assert "- Works as a developer." in text
    assert "User: ไปหาหมอ (meaning: I went to the doctor)" in text
    assert "User: how do I say tired" in text
    assert "Ploy (English help): Tired is เหนื่อย." in text
    assert "Ploy: วันนี้เป็นยังไงบ้างคะ (How was today?)" in text


def test_memory_system_prompt_requires_facts_json():
    assert '{"facts"' in MEMORY_SYSTEM_PROMPT
