"""The persona chat layer.

Trigger rules:
- DM: every text message (and stickers).
- Group: ONLY when the bot is @mentioned, or the message replies to one
  of the bot's messages. Everything else is ignored — this is what keeps
  LLM cost and group spam under control when 100 groups are active.

Concurrency: handlers never wait on each other across chats. A per-chat
asyncio.Lock serialises messages inside one chat (conversation order),
while the LLM semaphore in llm.py caps total parallel model calls. All
group replies are threaded (reply_to the triggering message).
"""
from __future__ import annotations

import asyncio
import logging
import random

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, ReplyParameters

from .. import db, gender, harvest, llm, pairs, sticker_picker, stickers, voice
from ..config import get_settings
from ..persona import BOT_NAME, FALLBACK_REPLIES, MOODS

log = logging.getLogger("bot.handlers.chat")
router = Router()

_locks: dict = {}
_me_cache: dict = {}


def _chat_lock(chat_id: int) -> asyncio.Lock:
    if len(_locks) > 5000:  # cheap cleanup so the dict cannot grow forever
        for cid in [c for c, lk in _locks.items() if not lk.locked()]:
            _locks.pop(cid, None)
    lock = _locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[chat_id] = lock
    return lock


async def _me(bot: Bot):
    me = _me_cache.get("me")
    if me is None:
        me = await bot.get_me()
        _me_cache["me"] = me
    return me


def _is_mentioned(message: Message, me) -> bool:
    text = message.text or ""
    for ent in message.entities or []:
        if ent.type == "mention":
            try:
                if ent.extract_from(text).lower() == ("@" + me.username).lower():
                    return True
            except Exception:
                continue
    return False


def _is_reply_to_bot(message: Message, me) -> bool:
    rt = message.reply_to_message
    return bool(rt and rt.from_user and rt.from_user.id == me.id)


async def _update_summary(chat_id: int, user_id: int, old_summary: str, dropped: list) -> None:
    try:
        new_summary = await llm.summarize(old_summary, dropped)
        if new_summary and new_summary != old_summary:
            await db.update_summary(chat_id, user_id, new_summary)
    except Exception:
        log.exception("summary update failed")


async def _respond(message: Message, bot: Bot, user_text: str, sticker_boost: float = 0.0) -> None:
    """One full chat exchange: memory -> LLM -> text/sticker out."""
    user = message.from_user
    chat_id = message.chat.id
    is_group = message.chat.type != "private"

    user_row = await db.get_or_create_user(user.id, user.username, user.first_name)
    summary, history = await db.get_history(chat_id, user.id)
    transcript: list = []
    if is_group:
        # Shared group transcript — the chats row keyed (chat_id, 0).
        # One timeline for the whole group, so the LLM can follow a
        # conversation between several people instead of only this one
        # user's thread. Fetched BEFORE the current line is appended,
        # exactly like the per-user history above.
        try:
            _t_summary, transcript = await db.get_history(chat_id, 0)
        except Exception:
            log.exception("group transcript fetch failed — continuing without it")
            transcript = []
    dropped = await db.append_message(chat_id, user.id, "user", user_text)
    if is_group:
        sender = user.first_name or "User"
        await db.append_message(chat_id, 0, "user", sender + ": " + user_text)

    # Gender: detected deterministically from the user's own Hinglish
    # grammar (app/gender.py) — the prompt rule alone kept slipping,
    # so the LLM gets it as a FACT line. Sticky on the users row: this
    # message first, then (only while still unknown) recent history.
    # Sticker triggers send synthetic pseudo-text; those skip detection.
    synthetic = user_text.startswith("[user ne")
    user_gender = user_row.get("gender")
    if not synthetic:
        detected = gender.detect_gender(user_text)
        if detected is None and user_gender is None:
            past_texts = [
                str(m.get("text", ""))
                for m in reversed(history)
                if m.get("role") == "user"
            ]
            detected = gender.detect_gender_in_texts(past_texts)
        merged = gender.resolve_gender(user_gender, detected)
        if merged is not None and merged != user_gender:
            await db.set_user_gender(user.id, merged)
            user_gender = merged

    # 1) DB-trained pairs layer — a hit answers with ZERO LLM requests.
    #    (Sticker triggers send synthetic pseudo-text; those skip the layer.)
    result = None
    if not synthetic:
        try:
            hit = await pairs.lookup(chat_id, user_text)
        except Exception:
            log.exception("pairs lookup failed — falling through to LLM")
            hit = None
        if hit:
            log.info("pair hit (%s) in chat %s", hit.get("via", "exact"), chat_id)
            result = {"reply": hit["reply"], "mood": hit["mood"], "sticker": False}
        else:
            log.info("pair miss -> LLM in chat %s", chat_id)

    # 2) LLM on a miss — and the fresh exchange is learned back ('auto'),
    #    so the same sawal is free next time. Quota-fallback lines are
    #    NOT learned (they are generic, not real answers).
    if result is None:
        extra_context = None
        if is_group and transcript:
            # User lines already carry their sender's name — print
            # them verbatim; bot lines get the bot's name so the model
            # can tell who said what.
            extra_context = [
                str(m.get("text", "")) if m.get("role") == "user"
                else BOT_NAME + ": " + str(m.get("text", ""))
                for m in transcript
            ]
        result = await llm.chat_reply(
            summary, history, user_text, user.first_name or "",
            gender=user_gender, extra_context=extra_context,
        )
        if not synthetic and result["reply"] not in FALLBACK_REPLIES:
            try:
                await pairs.learn(
                    chat_id, user_text, result["reply"], source="auto",
                    created_by=user.id, mood=result.get("mood"),
                )
            except Exception:
                log.exception("pairs learn failed")
    reply_text = result["reply"]
    mood = result["mood"]
    await db.append_message(chat_id, user.id, "bot", reply_text)
    if is_group:
        await db.append_message(chat_id, 0, "bot", reply_text)

    if dropped:
        asyncio.create_task(_update_summary(chat_id, user.id, summary, dropped))

    reply_params = ReplyParameters(message_id=message.message_id) if is_group else None

    # Voice notes: planned differentiator, stub returns False while off.
    if await voice.maybe_send_voice_reply(bot, chat_id, reply_text, message.message_id):
        return

    # Sticker choice (owner's rule): a sticker is picked ONLY when the
    # LLM flagged one, or the user just sent a sticker (sticker_boost)
    # and the dice land under 0.12 + boost. In groups, a sticker this
    # group itself taught us (harvested associations) also counts when
    # it fires. There is no plain random sticker on ordinary text.
    wants_sticker = bool(result.get("sticker")) or (
        sticker_boost > 0 and random.random() < 0.12 + sticker_boost
    )
    sticker_file = None
    if is_group and not synthetic:
        # A sticker this group itself taught us (harvested associations)
        # beats the generic mood pool when one matches this message.
        try:
            sticker_file = await harvest.pick_for_group(chat_id, user_text)
        except Exception:
            log.exception("harvested sticker pick failed")
            sticker_file = None
    if sticker_file is None and wants_sticker:
        sticker_file = await stickers.pick_for_mood(mood)

    if sticker_file:
        # A chosen sticker IS the whole visible reply — never text with
        # a sticker stuck on top. reply_text still lives in the history
        # and in the learned pairs above; only the visible send differs.
        await bot.send_sticker(chat_id, sticker_file, reply_parameters=reply_params)
        return

    await bot.send_message(chat_id, reply_text, reply_parameters=reply_params)


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    try:
        await _handle_text(message, bot)
    except Exception:
        log.exception("text handler failed")


async def _handle_text(message: Message, bot: Bot) -> None:
    text = message.text or ""
    if text.startswith("/"):
        return  # commands belong to the other routers
    user = message.from_user
    if user is None or user.is_bot:
        return

    if message.chat.type == "private":
        # Fallback seeding route: admin replies to a sticker with just a mood word.
        settings = get_settings()
        rt = message.reply_to_message
        if (
            user.id == settings.admin_id
            and rt is not None
            and rt.sticker is not None
            and stickers.is_valid_mood(text)
        ):
            mood = text.strip().lower()
            await stickers.save_sticker(rt.sticker.file_id, mood, user.id)
            await message.answer("✅ Saved to the sticker pool — mood: <b>" + mood + "</b>")
            return
        async with _chat_lock(message.chat.id):
            await _respond(message, bot, text)
        return

    # Group: only mention or reply-to-bot triggers a chat reply.
    me = await _me(bot)
    if not (_is_mentioned(message, me) or _is_reply_to_bot(message, me)):
        return
    clean = text.replace("@" + me.username, "").strip() or text
    async with _chat_lock(message.chat.id):
        await _respond(message, bot, clean)


@router.callback_query(F.data.startswith(sticker_picker.CALLBACK_PREFIX + ":"))
async def on_sticker_pick(callback: CallbackQuery, bot: Bot) -> None:
    try:
        await _handle_sticker_pick(callback, bot)
    except Exception:
        log.exception("sticker pick handler failed")


async def _handle_sticker_pick(callback: CallbackQuery, bot: Bot) -> None:
    """One tap on a sticker-mood prompt (see sticker_picker).

    Only the owner's tap counts — anyone else's is a silent no-op and
    does NOT consume the pending entry. A valid tap pops the token,
    saves through the same stickers.save_sticker path the caption flow
    uses, and edits the prompt into its final text (buttons removed).
    """
    parsed = sticker_picker.parse_callback(callback.data or "")
    if parsed is None:
        await callback.answer()
        return
    token, choice = parsed
    if choice != sticker_picker.CANCEL_CHOICE and choice not in MOODS:
        await callback.answer()
        return
    settings = get_settings()
    if callback.from_user is None or callback.from_user.id != settings.admin_id:
        await callback.answer()
        return
    file_id = sticker_picker.resolve(token)
    if file_id is None:
        await callback.answer("This one expired — send the sticker again.")
        return
    prompt = callback.message
    if choice == sticker_picker.CANCEL_CHOICE:
        if prompt is not None:
            await bot.edit_message_text(
                "❌ Skipped",
                chat_id=prompt.chat.id,
                message_id=prompt.message_id,
                reply_markup=sticker_picker.cleared_markup(),
            )
        await callback.answer()
        return
    await stickers.save_sticker(file_id, choice, callback.from_user.id)
    if prompt is not None:
        await bot.edit_message_text(
            "✅ Saved to " + choice.capitalize() + " " + sticker_picker.MOOD_EMOJI[choice],
            chat_id=prompt.chat.id,
            message_id=prompt.message_id,
            reply_markup=sticker_picker.cleared_markup(),
        )
    await callback.answer()


@router.message(F.sticker)
async def on_sticker(message: Message, bot: Bot) -> None:
    try:
        await _handle_sticker(message, bot)
    except Exception:
        log.exception("sticker handler failed")


async def _handle_sticker(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or user.is_bot or message.sticker is None:
        return
    settings = get_settings()

    if message.chat.type == "private" and user.id == settings.admin_id:
        caption = (message.caption or "").strip().lower()
        if caption in MOODS:
            await stickers.save_sticker(message.sticker.file_id, caption, user.id)
            await message.answer("✅ Saved to the sticker pool — mood: <b>" + caption + "</b>")
        elif caption:
            await message.answer(
                "That's not a mood 😅 Put one of these in the caption: " + ", ".join(MOODS)
            )
        else:
            # No caption: offer the mood buttons instead of instructions.
            # Each sticker gets its own token + prompt, so the owner can
            # send a batch back-to-back and tap moods in any order.
            token = sticker_picker.stash(message.sticker.file_id)
            await message.answer(
                "Which mood should this sticker go to?",
                reply_markup=sticker_picker.build_keyboard(token),
            )
        return

    if message.chat.type != "private":
        # In groups a random sticker is not a trigger — only a reply to the bot is.
        me = await _me(bot)
        if not _is_reply_to_bot(message, me):
            return

    emoji = message.sticker.emoji or "🙂"
    async with _chat_lock(message.chat.id):
        await _respond(
            message, bot, "[user ne ek sticker bheja: " + emoji + "]", sticker_boost=0.45
        )
