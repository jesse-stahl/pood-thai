import pytest

from pood_thai.models import BrainError, ReviewItem, Turn
from pood_thai.turns import TurnParseError, parse_facts, parse_review, parse_turn

FULL = '{"action": "reply", "user_english": "I am tired", "english_note": "", "thai": "เหนื่อยเหรอคะ", "english": "Tired?"}'


def test_parse_plain_json():
    assert parse_turn(FULL) == Turn("reply", "I am tired", "", "เหนื่อยเหรอคะ", "Tired?")


def test_parse_json_inside_code_fence_and_prose():
    raw = "Here you go:\n```json\n" + FULL + "\n```"
    assert parse_turn(raw).thai == "เหนื่อยเหรอคะ"


def test_unknown_action_becomes_reply_and_missing_fields_are_empty():
    turn = parse_turn('{"action": "dance", "thai": "ค่ะ"}')
    assert turn == Turn("reply", "", "", "ค่ะ", "")


def test_repeat_actions_allow_empty_text():
    assert parse_turn('{"action": "repeat_slower", "thai": ""}').action == "repeat_slower"
    assert parse_turn('{"action": "repeat"}').action == "repeat"


def test_non_json_text_is_used_as_thai():
    assert parse_turn("สวัสดีค่ะ") == Turn("reply", "", "", "สวัสดีค่ะ", "")


def test_empty_reply_raises_turn_parse_error_which_is_a_brain_error():
    with pytest.raises(TurnParseError):
        parse_turn("   ")
    with pytest.raises(BrainError):
        parse_turn('{"action": "reply", "thai": ""}')


def test_english_note_alone_is_a_valid_reply():
    assert parse_turn('{"action": "reply", "english_note": "Tired is เหนื่อย."}').english_note


def test_parse_facts_filters_and_caps():
    raw = '{"facts": [" A. ", "", 3, "B."]}'
    assert parse_facts(raw) == ["A.", "B."]
    many = '{"facts": [' + ",".join(f'"F{i}."' for i in range(40)) + "]}"
    assert len(parse_facts(many)) == 30


def test_parse_facts_rejects_garbage():
    with pytest.raises(TurnParseError):
        parse_facts("no json here")
    with pytest.raises(TurnParseError):
        parse_facts('{"facts": "nope"}')


def test_malformed_json_is_not_spoken_as_thai():
    with pytest.raises(TurnParseError):
        parse_turn('{"action": "reply", "thai": "สวัสดีค่ะ"')


def test_parse_facts_with_only_invalid_entries_is_an_error_not_an_empty_list():
    with pytest.raises(TurnParseError):
        parse_facts('{"facts": [null, 42, {}]}')
    assert parse_facts('{"facts": []}') == []


def test_parse_review_maps_items_by_number_and_keeps_the_learners_text():
    sentences = ["ไปตลาดครับ", "ซื้อมะม่วงสองครับ", "อร่อยครับ"]
    raw = (
        '{"items": [{"n": 2, "ok": false, "better": "ซื้อมะม่วงสองลูกครับ", "why": "Add the classifier ลูก."},'
        ' {"n": 1, "ok": true}], "tips": ["Use classifiers with numbers.", "Nice job!", "extra"]}'
    )
    items, tips = parse_review(raw, sentences)
    assert items == (
        ReviewItem("ไปตลาดครับ", True),
        ReviewItem("ซื้อมะม่วงสองครับ", False, "ซื้อมะม่วงสองลูกครับ", "Add the classifier ลูก."),
        ReviewItem("อร่อยครับ", True),
    )
    assert tips == ("Use classifiers with numbers.", "Nice job!")


def test_parse_review_treats_fixes_without_a_new_version_as_ok():
    raw = '{"items": [{"n": 1, "ok": false, "better": ""}, {"n": 2, "ok": false, "better": "เหมือนเดิม"}], "tips": "nope"}'
    items, tips = parse_review(raw, ["ก", "เหมือนเดิม"])
    assert items == (ReviewItem("ก", True), ReviewItem("เหมือนเดิม", True))
    assert tips == ()


def test_parse_review_rejects_garbage():
    with pytest.raises(TurnParseError):
        parse_review("no json here", ["ก"])
    with pytest.raises(TurnParseError):
        parse_review('{"items": "x"}', ["ก"])


def test_parse_review_ignores_spacing_only_changes_and_boolean_numbers():
    raw = '{"items": [{"n": true, "ok": false, "better": "x", "why": "?"}, {"n": 2, "ok": false, "better": "ไปตลาดครับ"}]}'
    items, _ = parse_review(raw, ["ก", "ไป ตลาด ครับ"])
    assert items == (ReviewItem("ก", True), ReviewItem("ไป ตลาด ครับ", True))


def test_parse_review_accepts_numbers_sent_as_strings():
    raw = '{"items": [{"n": "1", "ok": false, "better": "ซื้อมะม่วงสองลูกครับ", "why": "Add ลูก."}], "tips": []}'
    items, _ = parse_review(raw, ["ซื้อมะม่วงสองครับ"])
    assert items == (ReviewItem("ซื้อมะม่วงสองครับ", False, "ซื้อมะม่วงสองลูกครับ", "Add ลูก."),)


def test_parse_review_fails_when_no_entry_matches_a_sentence():
    for raw in ('{"items": [], "tips": []}', '{"items": [{"ok": false, "better": "x"}]}', '{"items": [{"n": 0, "ok": true}]}'):
        with pytest.raises(TurnParseError):
            parse_review(raw, ["ก", "ข"])
    assert parse_review('{"items": [], "tips": []}', []) == ((), ())


def test_parse_review_rejects_shifted_or_out_of_range_numbering():
    zero_based = '{"items": [{"n": 0, "ok": true}, {"n": 1, "ok": false, "better": "ซื้อมะม่วงสองลูกครับ", "why": "x"}]}'
    too_high = '{"items": [{"n": 1, "ok": true}, {"n": 3, "ok": true}]}'
    for raw in (zero_based, too_high):
        with pytest.raises(TurnParseError):
            parse_review(raw, ["ไปตลาดครับ", "ซื้อมะม่วงสองครับ"])


def test_parse_review_ignores_digit_like_symbols_and_reads_thai_digits():
    with pytest.raises(TurnParseError):
        parse_review('{"items": [{"n": "²", "ok": true}]}', ["ก"])
    items, _ = parse_review('{"items": [{"n": "๑", "ok": false, "better": "ข", "why": "y"}]}', ["ก"])
    assert items == (ReviewItem("ก", False, "ข", "y"),)
