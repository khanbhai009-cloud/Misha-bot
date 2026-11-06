"""/start, info menus and the group welcome message."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db, pairs
from ..config import get_settings
from ..persona import BOT_NAME

log = logging.getLogger("bot.handlers.common")
router = Router()


def mention(user_id: int, name) -> str:
    """Clickable user mention that works without a username."""
    return '<a href="tg://user?id=' + str(user_id) + '">' + escape(name or "User") + "</a>"


def stats_card(u: dict) -> str:
    prot = ""
    until = u.get("protected_until")
    if until and until > datetime.now(timezone.utc):
        prot = "\n🛡 Protection: ON (safe from rob/kill)"
    coins = "{:,}".format(int(u.get("coins") or 0))
    gems = u.get("gems") or 0
    lines = [
        "💰 Balance: <b>" + coins + "</b> coins",
        "💎 Gems: <b>" + str(gems) + "</b>",
        "⭐ XP: <b>" + str(u.get("xp") or 0) + "</b>",
        "🔪 Kills: <b>" + str(u.get("kills") or 0) + "</b>",
    ]
    return "\n".join(lines) + prot


def start_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍀 Features", callback_data="info:features"),
         InlineKeyboardButton(text="🎮 Games", callback_data="info:games")],
        [InlineKeyboardButton(text="💰 Economy", callback_data="info:economy")],
        [InlineKeyboardButton(text="➕ Add me to your group",
                                url="https://t.me/" + bot_username + "?startgroup=true")],
    ])


FEATURES_TEXT = (
    "🍀 <b>" + BOT_NAME + "'s features</b>\n\n"
    "💬 <b>Chat</b> — say anything in DM; in a group, tag @" + BOT_NAME + " or reply to her message\n"
    "💰 <b>Economy</b> — /daily, /bal, /rob, /kill, /protect, /give, /toprich\n"
    "🎮 <b>Games</b> — /bomb group game, /truth, /dare, /joke\n"
    "🖼 <b>/own</b> — reply to any sticker with /own and it becomes your own sticker pack\n"
    "🎭 Sometimes she replies with a sticker too, depending on her mood"
)

GAMES_TEXT = (
    "🎮 <b>Games</b>\n\n"
    "💣 <b>/bomb &lt;amount&gt;</b> — group game: everyone stakes the same amount, the bomb keeps "
    "getting passed, and whoever is holding it when it explodes is out. The last player standing "
    "wins the whole pot (5% house cut).\n"
    "😜 <b>/truth</b>, <b>/dare</b>, <b>/joke</b> — free fun, in groups or DM."
)

ECONOMY_TEXT = (
    "💰 <b>Economy</b>\n\n"
    "🎁 /daily — 2000 coins + 50 XP every day (DM only)\n"
    "👤 /bal — your stats card\n"
    "⚔️ /rob — reply to someone to loot them (10% tax, can fail too)\n"
    "🔪 /kill — earn 100–200 coins, 5% of the target's coins burn\n"
    "🛡 /protect 1d (400) or 2d (1000) — safety from rob/kill\n"
    "🎁 /give — give coins to someone (10% tax)\n"
    "🏆 /toprich — the 10 richest players"
)

_INFO = {"features": FEATURES_TEXT, "games": GAMES_TEXT, "economy": ECONOMY_TEXT}


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    u = await db.get_or_create_user(user.id, user.username, user.first_name)
    me = await bot.get_me()
    text = (
        "💙 Hieeee " + escape(user.first_name or "friend") + "! I'm <b>" + BOT_NAME + "</b> — "
        "your buddy for chatting, gaming and fun.\n\n"
        "📊 <b>Your stats:</b>\n" + stats_card(u) + "\n\n"
        "👇 Choose an option:"
    )
    await message.answer(text, reply_markup=start_keyboard(me.username))


@router.message(Command("teach"))
async def cmd_teach(message: Message, command: CommandObject) -> None:
    """Owner-only: hand-teach a global stimulus -> response pair.

    Format: /teach <sawal> => <jawab>
    """
    settings = get_settings()
    if message.from_user is None or settings.admin_id == 0 or message.from_user.id != settings.admin_id:
        await message.reply("This is only for my owner 😌 you just chat with me!")
        return
    raw = (command.args or "").strip()
    if "=>" not in raw:
        await message.reply(
            "📌 <b>Usage:</b> /teach &lt;question&gt; =&gt; &lt;answer&gt;\n"
            "Example: /teach tum khana khati ho =&gt; haan! biryani meri favourite hai 😋"
        )
        return
    stimulus, response = [part.strip() for part in raw.split("=>", 1)]
    if not stimulus or not response:
        await message.reply("📌 Write both the question and the answer: /teach &lt;question&gt; =&gt; &lt;answer&gt;")
        return
    ok = await pairs.learn(
        message.chat.id, stimulus, response, source="teach",
        created_by=message.from_user.id,
    )
    if ok:
        await message.reply(
            "✅ <b>Learned!</b>\n❓ " + escape(stimulus) + "\n💬 " + escape(response)
            + "\n\n(This question will now be answered straight from the DB, no LLM — global)"
        )
    else:
        await message.reply("😬 Couldn't learn that — keep the question/answer a bit shorter and cleaner")


@router.callback_query(F.data.startswith("info:"))
async def on_info(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[1]
    text = _INFO.get(key)
    if text and isinstance(callback.message, Message):
        await callback.message.answer(text)
    await callback.answer()


@router.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    if not any(m.id == me.id for m in message.new_chat_members or []):
        return
    await message.answer(
        "👀 Hii everyone! 💕 I'm <b>" + BOT_NAME + "</b>, just joined this group.\n\n"
        "💬 To chat, tag me or reply to my message\n"
        "🎁 Free coins every day with /daily (in DM)\n"
        "💣 Start a group game with /bomb &lt;amount&gt;\n"
        "⚔️ Enjoy the economy with /rob and /kill\n"
        "Let's get the fun started! 🎉"
    )
