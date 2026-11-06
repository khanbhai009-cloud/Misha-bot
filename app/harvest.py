"""Silent group harvesting — the bot quietly learns from real group
conversations without ever speaking out of turn.

Once group privacy mode is off, the bot receives every group message.
A dispatcher middleware (registered in main.py) passes each message
through observe_message() BEFORE the normal handlers run; observation
never consumes the update and never replies or reacts.

What gets learned — and only this:

- TEXT PAIR: a message that REPLIES (reply_to_message) to another
  human's text message, from a different user, in a group/supergroup.
  stimulus = the replied-to text, response = the reply text. It goes
  through pairs.learn(source='auto'), so the existing safety filter
  (links, phone numbers, gaaliyan) applies unchanged, and the pair is
  scoped to that chat like all auto-learned pairs.
- TEXT -> STICKER: a sticker sent as a reply to a text message. The
  association (normalised text -> sticker file_id) lands in
  harvested_stickers; chat.py uses it when replying in that group.
- STICKER -> STICKER: a sticker sent as a reply to a sticker. Stored
  the same way (file_id -> file_id) so reply-chain trends are visible
  in the admin panel.

Never learned: the bot's own messages, other bots' messages, replies
to the bot or messages mentioning the bot (those conversations belong
to the chat handler), commands (as stimulus or response), self-replies,
private chats, channel posts and edited messages (the middleware only
sees fresh `message` updates).

Only Telegram file_id strings are ever stored — never sticker bytes —
the same rule as the owner sticker pool.
"""
from __future__ import annotations

import logging
import random

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import Message

from . import db, pairs

log = logging.getLogger("bot.harvest")

# Probability that a group reply with a harvested sticker match
# actually sends the sticker (keeps the persona from spamming it).
HARVESTED_STICKER_CHANCE = 0.28

_me_cache: dict = {}
_last_sent: dict = {}


def plan_harvest(ev: dict) -> dict | None:
    """Decide what one observed message teaches us. Pure function.

    `ev` is a plain dict built by _event_from_message():
      chat_type, sender_id, sender_is_bot, text, sticker_file_id,
      mentions_bot, bot_id, and reply = the same sender/text/sticker
      fields for the replied-to message (or None).
    Returns a plan dict (kind + payload) or None to learn nothing.
    """
    if ev.get("chat_type") not in ("group", "supergroup"):
        return None
    reply = ev.get("reply")
    if not reply:
        return None
    bot_id = ev.get("bot_id")
    sender_id = ev.get("sender_id")
    orig_id = reply.get("sender_id")
    if sender_id is None or orig_id is None:
        return None
    if ev.get("sender_is_bot") or reply.get("sender_is_bot"):
        return None
    if sender_id == bot_id or orig_id == bot_id:
        return None
    if sender_id == orig_id:
        return None
    if ev.get("mentions_bot") or reply.get("mentions_bot"):
        return None

    reply_text = reply.get("text") or ""
    my_text = ev.get("text") or ""
    my_sticker = ev.get("sticker_file_id")

    # STICKER -> STICKER chain.
    if reply.get("sticker_file_id") and my_sticker:
        return {
            "kind": "sticker_sticker",
            "stimulus_file_id": reply["sticker_file_id"],
            "file_id": my_sticker,
        }

    if not reply_text or reply_text.lstrip().startswith("/"):
        return None

    # TEXT -> STICKER.
    if my_sticker:
        return {
            "kind": "text_sticker",
            "stimulus_text": reply_text,
            "file_id": my_sticker,
        }

    # TEXT PAIR.
    if my_text and not my_text.lstrip().startswith("/"):
        return {
            "kind": "text_pair",
            "stimulus_text": reply_text,
            "response_text": my_text,
        }
    return None


def _mentions_bot(text: str, bot_username: str | None) -> bool:
    if not text or not bot_username:
        return False
    return ("@" + bot_username).lower() in text.lower()


def _side(message: Message | None, bot_username: str | None) -> dict | None:
    if message is None:
        return None
    user = message.from_user
    return {
        "sender_id": user.id if user else None,
        "sender_is_bot": bool(user and user.is_bot),
        "text": message.text or "",
        "sticker_file_id": message.sticker.file_id if message.sticker else None,
        "mentions_bot": _mentions_bot(message.text or "", bot_username),
    }


def _event_from_message(message: Message, bot_id: int, bot_username: str | None) -> dict:
    ev = _side(message, bot_username) or {}
    ev["chat_type"] = message.chat.type if message.chat else None
    ev["bot_id"] = bot_id
    ev["reply"] = _side(message.reply_to_message, bot_username)
    return ev


async def _me(bot: Bot):
    me = _me_cache.get("me")
    if me is None:
        me = await bot.get_me()
        _me_cache["me"] = me
    return me


async def observe_message(message: Message, bot: Bot) -> None:
    """Persist whatever one group message teaches us. Never raises,
    never sends anything."""
    try:
        if message.chat is None or message.chat.type not in ("group", "supergroup"):
            return
        me = await _me(bot)
        plan = plan_harvest(_event_from_message(message, me.id, me.username))
        if plan is None:
            return
        chat_id = message.chat.id
        scope = str(chat_id)
        kind = plan["kind"]
        if kind == "text_pair":
            await pairs.learn(
                chat_id, plan["stimulus_text"], plan["response_text"], source="auto"
            )
        elif kind == "text_sticker":
            stimulus = plan["stimulus_text"]
            if pairs.is_unsafe_text(stimulus):
                return
            norm = pairs.normalize(stimulus)
            if not norm or len(norm) > 300:
                return
            await db.record_harvested_sticker(
                scope, "text", norm, plan["file_id"],
                stimulus_raw=stimulus[:300],
            )
        elif kind == "sticker_sticker":
            await db.record_harvested_sticker(
                scope, "sticker", plan["stimulus_file_id"], plan["file_id"]
            )
    except Exception:
        log.exception("harvest observation failed")


class HarvestMiddleware(BaseMiddleware):
    """Outer middleware on dp.message: observe every fresh message,
    then let the normal handlers run untouched."""

    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        if bot is not None and isinstance(event, Message):
            await observe_message(event, bot)
        return await handler(event, data)


async def pick_for_group(chat_id: int, user_text: str) -> str | None:
    """A harvested sticker for this group message, or None.

    Fuzzy-matches the normalised text against the chat scope's
    harvested text stimuli using the same RapidFuzz scoring as the
    pairs layer, then — only ~28% of the time — returns the most
    observed sticker for the matched stimulus. The same sticker is
    never returned twice in a row in one chat when an alternative
    exists.
    """
    if random.random() >= HARVESTED_STICKER_CHANCE:
        return None
    norm = pairs.normalize(user_text)
    if not norm:
        return None
    scope = str(chat_id)
    keys = await db.harvested_stimulus_keys(scope, "text")
    if not keys:
        return None
    best = pairs.fuzzy_best(norm, keys)
    if not best or best[1] < pairs.RF_THRESHOLD:
        return None
    responses = await db.harvested_responses(scope, "text", best[0])
    if not responses:
        return None
    last = _last_sent.get(chat_id)
    fresh = [r for r in responses if r["file_id"] != last]
    pool = fresh or responses
    weights = [int(r.get("hits") or 0) + 1 for r in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]["file_id"]
    if len(_last_sent) > 5000:
        _last_sent.clear()
    _last_sent[chat_id] = chosen
    return chosen
