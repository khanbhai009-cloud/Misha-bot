"""/own — user-owned sticker packs (Baka-style).

Usage (reply to any sticker):
  /own        -> save it to your latest pack (creates pack #1 if none)
  /own new    -> create a fresh pack and save it there
  /own 2      -> save it to pack #2 (created on first use)
  /own list   -> your pack links

Pack names follow Telegram's rule: <slug>_<n>_by_<bot_username>.
The sticker's own format (static/animated/video) decides the pack format;
formats cannot be mixed inside one pack — if the target pack has a
different format, we point the user at /own new instead of failing raw.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InputSticker, Message

from .. import db

log = logging.getLogger("bot.handlers.own")
router = Router()

USAGE = (
    "📌 <b>/own kaise use karein</b>\n\n"
    "Kisi sticker par <b>reply</b> karke:\n"
    "• /own → latest pack me save\n"
    "• /own 2 → pack #2 me save\n"
    "• /own new → naya pack bana kar save\n\n"
    "Bina reply ke:\n"
    "• /own list → tumhare saare packs"
)


def _sticker_format(sticker) -> str:
    if sticker.is_video:
        return "video"
    if sticker.is_animated:
        return "animated"
    return "static"


def _pack_name(user_id: int, index: int, bot_username: str) -> str:
    slug = "misha" + str(user_id)
    return slug + "_" + str(index) + "_by_" + bot_username


async def _create_pack(bot: Bot, message: Message, index: int, fmt: str, sticker) -> dict:
    me = await bot.get_me()
    user = message.from_user
    name = _pack_name(user.id, index, me.username)
    title = ((user.first_name or "Misha") + " ke stickers #" + str(index))[:60]
    inp = InputSticker(
        sticker=sticker.file_id,
        format=fmt,
        emoji_list=[sticker.emoji or "🙂"],
    )
    await bot.create_new_sticker_set(
        user_id=user.id, name=name, title=title, stickers=[inp]
    )
    await db.create_pack_record(user.id, index, name, title, fmt)
    return {"pack_name": name, "format": fmt}


async def _save_to_pack(bot: Bot, message: Message, index: int) -> None:
    sticker = message.reply_to_message.sticker
    fmt = _sticker_format(sticker)
    pack = await db.get_pack(message.from_user.id, index)

    if pack is None:
        try:
            pack = await _create_pack(bot, message, index, fmt, sticker)
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "occupied" in text or "invalid" in text and "name" in text:
                # Pack exists on Telegram but not in our DB (e.g. DB reset):
                # recover by adopting it — add the sticker and record it.
                me = await bot.get_me()
                name = _pack_name(message.from_user.id, index, me.username)
                title = ((message.from_user.first_name or "Misha") + " ke stickers #" + str(index))[:60]
                try:
                    await bot.add_sticker_to_set(
                        user_id=message.from_user.id, name=name,
                        sticker=InputSticker(sticker=sticker.file_id, format=fmt,
                                             emoji_list=[sticker.emoji or "🙂"]),
                    )
                    await db.create_pack_record(message.from_user.id, index, name, title, fmt)
                    pack = {"pack_name": name, "format": fmt}
                except TelegramBadRequest:
                    await message.reply("😬 Pack banane me kuch gadbad ho gayi. Thodi der baad phir try karo!")
                    return
            else:
                log.exception("create_new_sticker_set failed")
                await message.reply("😬 Pack nahi ban paya: " + str(exc)[:150])
                return
        await message.reply(
            "✨ Naya pack #" + str(index) + " ban gaya aur sticker save ho gaya!\n"
            "👉 https://t.me/addstickers/" + pack["pack_name"]
        )
        return

    if pack.get("format") != fmt:
        await message.reply(
            "⚠️ Ye sticker " + fmt + " hai, par pack #" + str(index) + " " + str(pack.get("format"))
            + " format ka hai — format mix nahi hota. /own new se iske liye alag pack banao!"
        )
        return

    try:
        await bot.add_sticker_to_set(
            user_id=message.from_user.id,
            name=pack["pack_name"],
            sticker=InputSticker(
                sticker=sticker.file_id, format=fmt,
                emoji_list=[sticker.emoji or "🙂"],
            ),
        )
    except TelegramBadRequest as exc:
        text = str(exc).lower()
        if "full" in text:
            await message.reply(
                "📦 Pack #" + str(index) + " full ho gaya hai (Telegram ki limit). "
                "/own new likh kar naya pack shuru karo!"
            )
        elif "invalid" in text and "sticker" in text:
            await message.reply("😬 Ye sticker is pack me add nahi ho sakta (format/duplicate issue). Dusra sticker try karo!")
        else:
            log.exception("add_sticker_to_set failed")
            await message.reply("😬 Sticker save nahi hua: " + str(exc)[:150])
        return
    await message.reply(
        "✅ Sticker pack #" + str(index) + " me save ho gaya!\n"
        "👉 https://t.me/addstickers/" + pack["pack_name"]
    )


@router.message(Command("own"))
async def cmd_own(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None:
        return
    arg = (command.args or "").strip().lower()

    if arg == "list":
        packs = await db.get_user_packs(user.id)
        if not packs:
            await message.reply("📦 Abhi tumhara koi pack nahi hai.\n\n" + USAGE)
            return
        lines = ["📦 <b>Tumhare sticker packs:</b>"]
        for p in packs:
            lines.append(
                "• Pack #" + str(p["pack_index"]) + " (" + str(p["format"]) + "): "
                "https://t.me/addstickers/" + p["pack_name"]
            )
        await message.reply("\n".join(lines))
        return

    rt = message.reply_to_message
    if rt is None or rt.sticker is None:
        await message.reply(USAGE)
        return

    if arg == "" :
        packs = await db.get_user_packs(user.id)
        index = packs[-1]["pack_index"] if packs else 1
    elif arg == "new":
        packs = await db.get_user_packs(user.id)
        index = (packs[-1]["pack_index"] + 1) if packs else 1
    elif arg.isdigit() and int(arg) >= 1:
        index = int(arg)
    else:
        await message.reply(USAGE)
        return

    try:
        await _save_to_pack(message.bot, message, index)
    except Exception:
        log.exception("/own failed")
        await message.reply("😬 Kuch gadbad ho gayi — thodi der baad phir try karo!")
