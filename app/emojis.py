"""Owner-controlled emoji preferences for the persona.

Two lists, both managed by the bot owner from inside Telegram
(/emojiban, /emojiadd, /emojilist — see handlers/common.py) and stored
in the emoji_settings table:

- banned: emojis Misha must NEVER use. Enforced twice — as a hard line
  in the LLM system prompt and as a post-filter over outgoing text —
  so a model slip (or an old learned pair) cannot leak one.
- preferred: emojis she should reach for more often (prompt-only).

The pure helpers (split / prompt lines / strip) carry no DB or
Telegram dependency so they can be unit-tested directly. The settings
themselves are fetched through db.get_emoji_settings behind a short
in-memory cache, because the lookup runs on every chat reply; admin
commands invalidate the cache so a change applies immediately.
"""
from __future__ import annotations

import re
import time

_CACHE_TTL_SECONDS = 30
_cache: dict = {"at": 0.0, "banned": [], "preferred": []}


def normalize_emoji(emoji: str) -> str:
    """Storage/comparison form: variation selectors removed, so ❤ and
    ❤️ are the same emoji everywhere (the stickers mood map compares
    the same way)."""
    return (emoji or "").replace("\ufe0f", "").strip()


def _is_emoji_base(ch: str) -> bool:
    cp = ord(ch)
    if cp in (0xFE0F, 0x20E3, 0x200D):
        return False  # attachers (variation selector, keycap, ZWJ)
    if 0x1F3FB <= cp <= 0x1F3FF:
        return False  # skin tones glue onto the previous emoji
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x2300 <= cp <= 0x23FF
        or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139,
                  0x3030, 0x303D, 0x3297, 0x3299)
    )


def split_emojis(text: str) -> list:
    """Split a command argument into individual emojis.

    Accepts space/comma separated emojis as well as several stuck
    together ("😢😡🥺"); ZWJ sequences, skin tones, flags and keycaps
    stay glued to their base emoji. Non-emoji characters are dropped.
    """
    out: list = []
    cur = ""
    prev = ""
    for ch in text or "":
        if ch in " \t\r\n,;":
            if cur:
                out.append(cur)
                cur = ""
            prev = ""
            continue
        is_ri = 0x1F1E6 <= ord(ch) <= 0x1F1FF
        glued = (
            prev == "\u200d"  # ZWJ sequence: the next base joins the chain
            or (is_ri and len(cur) == 1 and 0x1F1E6 <= ord(cur) <= 0x1F1FF)
        )
        if cur and _is_emoji_base(ch) and not glued:
            out.append(cur)
            cur = ch
        elif _is_emoji_base(ch) or cur or ch in "#*0123456789":
            # Digits/#/* only START a chunk as a keycap candidate; the
            # filter below drops them again when no keycap mark follows.
            cur += ch
        # else: a stray modifier or letter before any emoji — drop it
        prev = ch
    if cur:
        out.append(cur)
    result: list = []
    for chunk in out:
        has_base = any(_is_emoji_base(c) for c in chunk)
        if (has_base or "\u20e3" in chunk) and chunk not in result:
            result.append(chunk)
    return result


def prompt_lines(banned: list, preferred: list) -> list:
    """System-prompt lines carrying the owner's emoji rules."""
    lines = []
    if banned:
        lines.append(
            "Owner's emoji rule — NEVER use these emojis in your reply, "
            "in any form: " + " ".join(banned) + "."
        )
    if preferred:
        lines.append(
            "Owner's emoji rule — the owner loves these emojis; use them "
            "often and naturally in your replies: " + " ".join(preferred) + "."
        )
    return lines


def strip_banned(text: str, banned: list) -> str:
    """Hard guarantee: remove every banned emoji from outgoing text.

    Both the stored form and its variation-selector twin are removed,
    so a banned ❤️ is caught whether the model wrote it with or
    without U+FE0F. May return "" when the reply was nothing but a
    banned emoji — callers substitute a fallback line.
    """
    if not text or not banned:
        return text
    out = text
    for emoji in sorted(banned, key=len, reverse=True):
        if not emoji:
            continue
        plain = emoji.replace("\ufe0f", "")
        # Longest form first: removing the bare base first would
        # orphan the variation selector of the ❤️ form in the text.
        if plain:
            out = out.replace(plain + "\ufe0f", "")
        out = out.replace(emoji, "")
        if plain and plain != emoji:
            out = out.replace(plain, "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out.strip()


async def get_settings_cached() -> tuple:
    """(banned, preferred) from the DB, cached for a few seconds.

    On any DB error the last known lists come back (empty at boot), so
    a settings hiccup can never break a chat reply.
    """
    now = time.monotonic()
    if _cache["at"] and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["banned"], _cache["preferred"]
    try:
        from . import db

        banned, preferred = await db.get_emoji_settings()
    except Exception:
        return _cache["banned"], _cache["preferred"]
    _cache.update(at=now, banned=list(banned), preferred=list(preferred))
    return _cache["banned"], _cache["preferred"]


def invalidate_cache() -> None:
    """Called by the admin commands so a change applies at once."""
    _cache["at"] = 0.0
