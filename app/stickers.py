"""Sticker pool service — the persona's sticker replies (Nezuko-style).

The pool lives in the `stickers` table, grouped by mood. Seeding: the
bot owner (ADMIN_ID) sends the bot a sticker in DM with the mood word as
the caption (e.g. "happy") and it is saved under that mood. If a mood's
pool is empty, the bot simply sends text — no crash, no placeholder.
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
