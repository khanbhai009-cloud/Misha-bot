"""Telegram Business support — Misha answering on the owner's own ID.

The owner linked Misha to his personal Telegram account through
Settings > Telegram Business > Chatbots (read + reply permissions).
Messages from those chats arrive as ordinary `message` updates that
carry a `business_connection_id`, and a reply only lands in the chat
when it is sent back WITH that same id — message.answer() cannot do
it, so this router intercepts business messages before every other
router (it is included FIRST in main.py) and sends through
bot.send_message(..., business_connection_id=...).

Scope rules (owner's design):
- ONLY the free-chat AI reply runs here. Economy / games / /own /
  /teach / admin commands never fire in a business chat: the sender
  is the owner's personal contact, not a bot user. A business message
  that starts with "/" is ignored outright.
- NOTHING is learned from business chats: no pairs.learn, no
  harvesting (the harvest middleware already skips private chats),
  no rows in `chats`, no economy user rows for the contacts. The
  learned-pairs layer is only READ (lookup), and conversation context
  is kept in memory only, so a restart forgets it.
- Replies go out as plain text (no sticker sends, no voice) — the
  persona's words, with the owner's emoji rules applied as usual.
- A connection that is disabled or lacks the reply permission is
  answered with silence.
"""
from __future__ import annotations

import asyncio
import logging
import random

from aiogram import Bot, F, Router
from aiogram.types import BusinessConnection, Message

from .. import db, emojis, gender, llm, pairs
from ..persona import FALLBACK_REPLIES

log = logging.getLogger("bot.handlers.business")
router = Router()

# In-memory conversation context per (connection, chat). Deliberately
# NOT the `chats` table: these are the owner's personal conversations
# and must not be persisted. Capped like the chat handler's locks.
_history: dict = {}
_locks: dict = {}
HISTORY_KEEP = 30


def connection_can_reply(conn: dict | None) -> bool:
    """Pure gate for one stored connection row: enabled AND allowed
    to reply. Anything else (missing row included) means silence."""
    return bool(conn and conn.get("is_enabled") and conn.get("can_reply"))


def _connection_flags(event: BusinessConnection) -> tuple:
    """(is_enabled, can_reply) from a BusinessConnection update.

    aiogram 3.13 (pinned) exposes can_reply directly on the event;
    newer Bot API shapes carry it inside a `rights` object instead —
    read both so a dependency bump cannot silently break the gate.
    """
    can_reply = getattr(event, "can_reply", None)
    rights = getattr(event, "rights", None)
    if rights is not None and getattr(rights, "can_reply", None) is not None:
        can_reply = rights.can_reply
    return bool(event.is_enabled), bool(can_reply)


def _chat_lock(key) -> asyncio.Lock:
    if len(_locks) > 1000:
        for k in [k for k, lk in _locks.items() if not lk.locked()]:
            _locks.pop(k, None)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _remember(key, role: str, text: str) -> None:
    if len(_history) > 1000:
        _history.clear()
    msgs = _history.setdefault(key, [])
    msgs.append({"role": role, "text": text})
    if len(msgs) > HISTORY_KEEP:
        del msgs[:-HISTORY_KEEP]


@router.business_connection()
async def on_business_connection(event: BusinessConnection) -> None:
    """Telegram sends the full connection state when the owner links
    Misha, changes permissions, or unlinks her — just persist it."""
    try:
        is_enabled, can_reply = _connection_flags(event)
        await db.upsert_business_connection(
            event.id, event.user.id, event.user_chat_id, is_enabled, can_reply,
        )
        log.info(
            "business connection %s stored: owner=%s enabled=%s can_reply=%s",
            event.id, event.user.id, is_enabled, can_reply,
        )
    except Exception:
        log.exception("business connection store failed")


async def _get_connection(bot: Bot, connection_id: str) -> dict | None:
    """Stored connection row, with a live-fetch fallback.

    The connection update can be missed (bot asleep on Render free
    when the owner linked her); then the first business message asks
    Telegram for the current state via getBusinessConnection and
    stores it, instead of staying mute forever.
    """
    conn = await db.get_business_connection(connection_id)
    if conn is not None:
        return conn
    try:
        live = await bot.get_business_connection(connection_id)
    except Exception:
        log.exception("get_business_connection failed for %s", connection_id)
        return None
    is_enabled, can_reply = _connection_flags(live)
    await db.upsert_business_connection(
        live.id, live.user.id, live.user_chat_id, is_enabled, can_reply,
    )
    return await db.get_business_connection(connection_id)


async def _business_reply(message: Message, bot: Bot, user_text: str) -> None:
    """One persona exchange for a business chat. Mirrors the free-chat
    pipeline (pairs lookup -> LLM -> emoji rules) minus every write:
    no learning, no stored history, no user rows."""
    user = message.from_user
    chat_id = message.chat.id
    connection_id = message.business_connection_id
    key = (connection_id, chat_id)
    history = list(_history.get(key, []))

    try:
        emoji_banned, emoji_preferred = await emojis.get_settings_cached()
    except Exception:
        log.exception("emoji settings fetch failed — continuing without them")
        emoji_banned, emoji_preferred = [], []

    # Gender from the customer's own grammar, in-memory only (the
    # normal flow persists it on a users row; business contacts get
    # no rows at all).
    user_gender = gender.detect_gender(user_text)
    if user_gender is None:
        past_texts = [
            str(m.get("text", ""))
            for m in reversed(history)
            if m.get("role") == "user"
        ]
        user_gender = gender.detect_gender_in_texts(past_texts)

    result = None
    try:
        hit = await pairs.lookup(chat_id, user_text)
    except Exception:
        log.exception("business pairs lookup failed — falling through to LLM")
        hit = None
    if hit:
        log.info("business pair hit (%s) in chat %s", hit.get("via", "exact"), chat_id)
        result = {"reply": hit["reply"], "mood": hit["mood"], "sticker": False}
    else:
        result = await llm.chat_reply(
            "", history, user_text, user.first_name or "",
            gender=user_gender,
            emoji_banned=emoji_banned, emoji_preferred=emoji_preferred,
        )
        # NOTE: deliberately no pairs.learn here — business chats
        # never teach Misha anything (owner's privacy rule).

    reply_text = result["reply"]
    if emoji_banned:
        reply_text = emojis.strip_banned(reply_text, emoji_banned)
        if not reply_text:
            reply_text = random.choice(FALLBACK_REPLIES)

    _remember(key, "user", user_text)
    _remember(key, "bot", reply_text)
    await bot.send_message(chat_id, reply_text, business_connection_id=connection_id)


@router.message(F.business_connection_id)
async def on_business_message(message: Message, bot: Bot) -> None:
    try:
        await _handle_business_message(message, bot)
    except Exception:
        log.exception("business message handler failed")


async def _handle_business_message(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    conn = await _get_connection(bot, message.business_connection_id)
    if not connection_can_reply(conn):
        return
    if message.text:
        if message.text.startswith("/"):
            # Commands never run in business chats — not economy, not
            # games, not even /start. Silence, by design.
            return
        async with _chat_lock((message.business_connection_id, message.chat.id)):
            await _business_reply(message, bot, message.text)
    elif message.sticker:
        emoji = message.sticker.emoji or "🙂"
        async with _chat_lock((message.business_connection_id, message.chat.id)):
            await _business_reply(
                message, bot, "[user ne ek sticker bheja: " + emoji + "]"
            )
    # Photos / voice / other content: no reply.
