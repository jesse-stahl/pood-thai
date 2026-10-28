"""SQLite storage for sessions, messages and memory facts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pood_thai.models import Fact, Lang, Message, Review, ReviewItem, Role

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    lang TEXT NOT NULL,
    thai TEXT NOT NULL DEFAULT '',
    english TEXT NOT NULL DEFAULT '',
    english_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    topic TEXT NOT NULL,
    created_at TEXT NOT NULL,
    items TEXT NOT NULL,
    tips TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    source_session_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        lang=row["lang"],
        thai=row["thai"],
        english=row["english"],
        english_note=row["english_note"],
        created_at=row["created_at"],
    )


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        fact=row["fact"],
        source_session_id=row["source_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        session_id=row["session_id"],
        topic=row["topic"],
        created_at=row["created_at"],
        items=tuple(ReviewItem(**item) for item in json.loads(row["items"])),
        tips=tuple(json.loads(row["tips"])),
    )


class Database:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create_session(self, topic: str) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions (topic, started_at) VALUES (?, ?)", (topic, _now())
            )
        return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), session_id))

    def session_ended_at(self, session_id: int) -> str | None:
        row = self._conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else row["ended_at"]

    def session_topic(self, session_id: int) -> str | None:
        row = self._conn.execute("SELECT topic FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else row["topic"]

    def add_review(
        self, session_id: int, topic: str, items: tuple[ReviewItem, ...], tips: tuple[str, ...]
    ) -> Review:
        items_json = json.dumps([asdict(i) for i in items], ensure_ascii=False)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO reviews (session_id, topic, created_at, items, tips) VALUES (?, ?, ?, ?, ?)",
                (session_id, topic, _now(), items_json, json.dumps(list(tips), ensure_ascii=False)),
            )
        review = self.get_review(int(cur.lastrowid))
        assert review is not None
        return review

    def get_review(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return None if row is None else _review(row)

    def list_reviews(self) -> list[Review]:
        rows = self._conn.execute("SELECT * FROM reviews ORDER BY id DESC").fetchall()
        return [_review(r) for r in rows]

    def add_message(
        self,
        session_id: int,
        role: Role,
        lang: Lang,
        thai: str = "",
        english: str = "",
        english_note: str = "",
    ) -> Message:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (session_id, role, lang, thai, english, english_note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, role, lang, thai, english, english_note, _now()),
            )
        row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _message(row)

    def list_messages(self, session_id: int) -> list[Message]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        return [_message(r) for r in rows]

    def delete_last_exchange(self, session_id: int) -> list[int]:
        """Delete the last user message and every message after it. Returns deleted ids."""
        row = self._conn.execute(
            "SELECT MAX(id) AS id FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
        ).fetchone()
        if row is None or row["id"] is None:
            return []
        ids = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND id >= ? ORDER BY id",
                (session_id, row["id"]),
            ).fetchall()
        ]
        with self._conn:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND id >= ?", (session_id, row["id"])
            )
        return ids

    def list_facts(self) -> list[Fact]:
        rows = self._conn.execute("SELECT * FROM memory_facts ORDER BY id").fetchall()
        return [_fact(r) for r in rows]

    def replace_facts(self, facts: list[str], source_session_id: int | None) -> list[Fact]:
        """Make the stored facts equal `facts`, keeping rows whose text is unchanged."""
        facts = list(dict.fromkeys(facts))  # drop duplicates, keep order
        wanted = set(facts)
        kept: set[str] = set()
        now = _now()
        with self._conn:
            for fact in self.list_facts():
                if fact.fact in wanted and fact.fact not in kept:
                    kept.add(fact.fact)
                else:
                    self._conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact.id,))
            for text in facts:
                if text not in kept:
                    self._conn.execute(
                        "INSERT INTO memory_facts (fact, source_session_id, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?)",
                        (text, source_session_id, now, now),
                    )
        order = {text: i for i, text in enumerate(facts)}
        return sorted(self.list_facts(), key=lambda f: order[f.fact])

    def delete_fact(self, fact_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
        return cur.rowcount > 0
