"""Button-based mood picker for seeding the sticker pool.

Flow: the owner sends a sticker in DM *without* a mood caption -> the
chat handler stashes the file_id here under a short random token and
replies with an inline keyboard (one button per mood). Tapping a mood
saves the sticker under that mood; tapping Cancel drops it.

Why tokens: Telegram caps callback_data at 64 bytes and sticker
file_ids are far longer than that, so the file_id never travels inside
a button — only the token does, and the token -> file_id map lives in
this module's memory. Each sticker gets its OWN token and its OWN
prompt message, so the owner can send many stickers back-to-back and
tap their moods in any order. Pending entries expire after ~15
minutes; they are memory-only by design — a restart just expires them
and the owner re-sends the sticker.
"""
from __future__ import annotations

import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .persona import MOODS

TTL_SECONDS = 15 * 60
_MAX_PENDING = 200

CALLBACK_PREFIX = "spk"
CANCEL_CHOICE = "x"

MOOD_EMOJI = {
    "happy": "😊",
    "laugh": "😂",
    "shy": "🙈",
    "sad": "😢",
    "angry": "😡",
    "love": "😍",
    "bored": "🥱",
}

# token -> {"file_id": str, "expires": monotonic deadline}
_pending: dict = {}


def _purge_expired(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    for token in [t for t, p in _pending.items() if p["expires"] <= now]:
        _pending.pop(token, None)


def stash(file_id: str) -> str:
    """Remember a sticker awaiting a mood tap; returns its token."""
    _purge_expired()
    while len(_pending) >= _MAX_PENDING:
        _pending.pop(next(iter(_pending)), None)  # oldest first
    token = secrets.token_hex(4)
    while token in _pending:
        token = secrets.token_hex(4)
    _pending[token] = {"file_id": file_id, "expires": time.monotonic() + TTL_SECONDS}
    return token


def resolve(token: str) -> str | None:
    """Pop a pending entry and return its file_id.

    One-shot: a second resolve of the same token returns None. None
    also means the token was unknown or had expired — the caller
    cannot (and need not) tell the two apart.
    """
    _purge_expired()
    entry = _pending.pop(token, None)
    return entry["file_id"] if entry else None


def parse_callback(data: str) -> tuple | None:
    """'spk:<token>:<mood|x>' -> (token, choice); None when malformed."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2]


def build_keyboard(token: str) -> InlineKeyboardMarkup:
    """One button per mood (3 per row); Cancel shares the last row."""
    buttons = [
        InlineKeyboardButton(
            text=MOOD_EMOJI[mood] + " " + mood.capitalize(),
            callback_data=CALLBACK_PREFIX + ":" + token + ":" + mood,
        )
        for mood in MOODS
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    cancel = InlineKeyboardButton(
        text="❌ Cancel",
        callback_data=CALLBACK_PREFIX + ":" + token + ":" + CANCEL_CHOICE,
    )
    if rows and len(rows[-1]) < 3:
        rows[-1].append(cancel)  # share the last row when it has room
    else:
        rows.append([cancel])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cleared_markup() -> InlineKeyboardMarkup:
    """An explicitly empty keyboard — strips the buttons off a prompt
    message when it is edited into its final confirmation text."""
    return InlineKeyboardMarkup(inline_keyboard=[])
