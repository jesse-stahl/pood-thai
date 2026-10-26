"""Parse Claude's raw text replies into typed values."""

from __future__ import annotations

import json
from typing import Any

from pood_thai.models import BrainError, Turn

_ACTIONS = ("reply", "repeat", "repeat_slower")
MAX_FACTS = 30


class TurnParseError(BrainError):
    """Claude's reply could not be turned into a usable value."""


def extract_json_object(raw: str) -> dict[str, Any]:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise TurnParseError("No JSON object in reply")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TurnParseError(f"Invalid JSON in reply: {exc}") from exc
    if not isinstance(data, dict):
        raise TurnParseError("Reply JSON is not an object")
    return data


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def parse_turn(raw: str) -> Turn:
    try:
        data = extract_json_object(raw)
    except TurnParseError:
        text = raw.strip()
        if not text:
            raise TurnParseError("Empty reply") from None
        return Turn(action="reply", user_english="", english_note="", thai=text, english="")
    action = data.get("action", "reply")
    if action not in _ACTIONS:
        action = "reply"
    turn = Turn(
        action=action,
        user_english=_text(data, "user_english"),
        english_note=_text(data, "english_note"),
        thai=_text(data, "thai"),
        english=_text(data, "english"),
    )
    if turn.action == "reply" and not turn.thai and not turn.english_note:
        raise TurnParseError("Reply has no text")
    return turn


def parse_facts(raw: str) -> list[str]:
    data = extract_json_object(raw)
    facts = data.get("facts")
    if not isinstance(facts, list):
        raise TurnParseError("'facts' is not a list")
    cleaned = [f.strip() for f in facts if isinstance(f, str) and f.strip()]
    return cleaned[:MAX_FACTS]
