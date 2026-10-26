import pytest

from pood_thai.models import BrainError, Turn
from pood_thai.turns import TurnParseError, parse_facts, parse_turn

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
