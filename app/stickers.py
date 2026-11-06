"""Sticker pool service — the persona's sticker replies (Nezuko-style).

The pool lives in the `stickers` table, grouped by mood. Seeding: the
bot owner (ADMIN_ID) sends the bot a sticker in DM — with the mood word
as the caption (e.g. "happy") it is saved under that mood directly;
without a caption the bot replies with mood buttons instead (see
sticker_picker). If a mood's pool is empty, the bot simply sends text —
no crash, no placeholder.
"""
from __future__ import annotations

from . import db
from .persona import MOODS


def is_valid_mood(text: str) -> bool:
    return bool(text) and text.strip().lower() in MOODS


async def save_sticker(file_id: str, mood: str, added_by: int) -> None:
    await db.add_sticker(file_id, mood.strip().lower(), added_by)


async def pick_for_mood(mood: str):
    """Random sticker file_id for a mood, or None if the pool is empty."""
    if mood not in MOODS:
        mood = "happy"
    return await db.random_sticker(mood)


# ---------------------------------------------------------------------------
# Emoji -> mood. Every Telegram sticker carries an emoji, so a user's
# sticker can be answered in kind: the reply sticker is picked from the
# pool of the SAME mood before the LLM's mood is tried (see the send
# block in handlers/chat.py). Judgement calls: 😊 reads shy on this
# persona (her seed intro line pairs it with the shy mood), 🥺 reads
# sad, 🙄 reads bored. Anything unmapped defaults to "happy".
EMOJI_MOODS = {
    "😍": "love", "🥰": "love", "❤️": "love", "💕": "love", "💖": "love",
    "😘": "love", "💋": "love", "🫶": "love",
    "😂": "laugh", "🤣": "laugh", "😆": "laugh", "😹": "laugh",
    "🙈": "shy", "😳": "shy", "😊": "shy", "☺️": "shy", "🌸": "shy",
    "😢": "sad", "😭": "sad", "😔": "sad", "💔": "sad", "🥺": "sad",
    "😡": "angry", "🤬": "angry", "😠": "angry", "💢": "angry",
    "🥱": "bored", "😴": "bored", "😐": "bored", "😑": "bored", "🙄": "bored",
    "😄": "happy", "😃": "happy", "✨": "happy", "🎉": "happy",
    "👍": "happy", "🔥": "happy", "💯": "happy",
}


def mood_for_emoji(emoji: str) -> str:
    """Persona mood for a sticker's emoji; unmapped emoji -> "happy".

    Compared with the variation selector (U+FE0F) stripped on both
    sides, so "❤" and "❤️" land on the same mood.
    """
    key = (emoji or "").strip().replace("\ufe0f", "")
    if not key:
        return "happy"
    for known, mood in EMOJI_MOODS.items():
        if known.replace("\ufe0f", "") == key:
            return mood
    return "happy"


# Moods strong enough that a text reply carrying one may be followed
# by a matching sticker (the follow-up gate in handlers/chat.py).
# happy and bored are too common to mean anything — they never fire it.
STRONG_MOODS = ("love", "laugh", "sad", "angry", "shy")
AFTER_TEXT_STICKER_CHANCE = 0.35
