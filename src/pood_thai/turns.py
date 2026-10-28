"""Parse Claude's raw text replies into typed values."""

from __future__ import annotations

import json
from typing import Any

from pood_thai.models import BrainError, ReviewItem, Turn

_ACTIONS = ("reply", "repeat", "repeat_slower")
MAX_FACTS = 30
MAX_TIPS = 2


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
        if "{" in text:
            # Malformed JSON: re-raise rather than read braces and keys aloud.
            raise
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
    if facts and not cleaned:
        raise TurnParseError("'facts' has no valid entries")
    return cleaned[:MAX_FACTS]


def parse_review(raw: str, sentences: list[str]) -> tuple[tuple[ReviewItem, ...], tuple[str, ...]]:
    """One item per learner sentence (matched by number); anything unclear counts as ok."""
    data = extract_json_object(raw)
    entries = data.get("items")
    if not isinstance(entries, list):
        raise TurnParseError("'items' is not a list")
    by_number = {e["n"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("n"), int)}
    items: list[ReviewItem] = []
    for n, said in enumerate(sentences, start=1):
        entry = by_number.get(n, {})
        better = _text(entry, "better")
        why = _text(entry, "why")
        if entry.get("ok") is False and better and better != said.strip():
            items.append(ReviewItem(said, False, better, why))
        else:
            items.append(ReviewItem(said, True))
    raw_tips = data.get("tips")
    tips = [t.strip() for t in raw_tips if isinstance(t, str) and t.strip()] if isinstance(raw_tips, list) else []
    return tuple(items), tuple(tips[:MAX_TIPS])
