"""Conversation topics shown on the start screen."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    thai: str
    hint: str


TOPICS: list[Topic] = [
    Topic("day", "Your day", "วันนี้ของคุณ", "what the user did today and how they feel"),
    Topic("food", "Food", "อาหาร", "what they ate, favourite dishes, cooking, restaurants"),
    Topic("weekend", "Weekend & plans", "วันหยุดและแผน", "last or next weekend, upcoming plans"),
    Topic("work", "Work", "งาน", "their job, colleagues, what they did at work"),
    Topic("family", "Family & friends", "ครอบครัวและเพื่อน", "the people in their life"),
    Topic("travel", "Travel", "การเดินทาง", "trips, places in Thailand, getting around"),
    Topic("hobbies", "Hobbies", "งานอดิเรก", "free time, sports, music, films"),
    Topic("free", "Free chat", "คุยอิสระ", "anything the user wants to talk about"),
]


def get_topic(topic_id: str) -> Topic:
    for topic in TOPICS:
        if topic.id == topic_id:
            return topic
    raise ValueError(f"Unknown topic: {topic_id}")
