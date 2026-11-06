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
        prot = "\n🛡 Protection: ON (rob/kill se safe)"
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
    "🍀 <b>" + BOT_NAME + " ke features</b>\n\n"
    "💬 <b>Chat</b> — DM me kuch bhi bolo, group me @" + BOT_NAME + " ko tag karo ya uske message par reply karo\n"
    "💰 <b>Economy</b> — /daily, /bal, /rob, /kill, /protect, /give, /toprich\n"
    "🎮 <b>Games</b> — /bomb group game, /truth, /dare, /joke\n"
    "🖼 <b>/own</b> — kisi sticker par reply karke /own likho, tumhara apna sticker pack ban jayega\n"
    "🎭 Mood ke hisaab se kabhi kabhi sticker se bhi jawab milega"
)

GAMES_TEXT = (
    "🎮 <b>Games</b>\n\n"
    "💣 <b>/bomb &lt;amount&gt;</b> — group game: sab same amount lagate hain, bomb pass hota rehta hai, "
    "jo phatne par pakda gaya woh out. Last bacha player pura pot jeet-ta hai (5% house cut).\n"
    "😜 <b>/truth</b>, <b>/dare</b>, <b>/joke</b> — bina paiso wala fun, group me ya DM me."
)

ECONOMY_TEXT = (
    "💰 <b>Economy</b>\n\n"
    "🎁 /daily — roz 2000 coins + 50 XP (sirf DM me)\n"
    "👤 /bal — apna stats card\n"
    "⚔️ /rob — kisi ko reply karke loot lo (10% tax, kabhi fail bhi)\n"
    "🔪 /kill — 100–200 coins kamao, target ke 5% coins jal jayenge\n"
    "🛡 /protect 1d (400) ya 2d (1000) — rob/kill se safety\n"
    "🎁 /give — kisi ko coins do (10% tax)\n"
    "🏆 /toprich — sabse ameer 10 log"
)

_INFO = {"features": FEATURES_TEXT, "games": GAMES_TEXT, "economy": ECONOMY_TEXT}


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    u = await db.get_or_create_user(user.id, user.username, user.first_name)
    me = await bot.get_me()
    text = (
        "💙 Hieeee " + escape(user.first_name or "dost") + "! Main hu <b>" + BOT_NAME + "</b> — "
        "chatting, gaming aur masti wali dost.\n\n"
        "📊 <b>Tumhare stats:</b>\n" + stats_card(u) + "\n\n"
        "👇 Option choose karo:"
    )
    await message.answer(text, reply_markup=start_keyboard(me.username))


@router.message(Command("teach"))
async def cmd_teach(message: Message, command: CommandObject) -> None:
    """Owner-only: hand-teach a global stimulus -> response pair.

    Format: /teach <sawal> => <jawab>
    """
    settings = get_settings()
    if message.from_user is None or settings.admin_id == 0 or message.from_user.id != settings.admin_id:
        await message.reply("Ye sirf mere owner ke liye hai 😌 tum bas baat karo mujhse!")
        return
    raw = (command.args or "").strip()
    if "=>" not in raw:
        await message.reply(
            "📌 <b>Usage:</b> /teach &lt;sawal&gt; =&gt; &lt;jawab&gt;\n"
            "Jaise: /teach tum khana khati ho =&gt; haan! biryani meri favourite hai 😋"
        )
        return
    stimulus, response = [part.strip() for part in raw.split("=>", 1)]
    if not stimulus or not response:
        await message.reply("📌 Sawal aur jawab dono likho: /teach &lt;sawal&gt; =&gt; &lt;jawab&gt;")
        return
    ok = await pairs.learn(
        message.chat.id, stimulus, response, source="teach",
        created_by=message.from_user.id,
    )
    if ok:
        await message.reply(
            "✅ <b>Seekh liya!</b>\n❓ " + escape(stimulus) + "\n💬 " + escape(response)
            + "\n\n(Ab ye sawal bina LLM ke, seedha DB se jawab milega — global)"
        )
    else:
        await message.reply("😬 Seekh nahi payi — sawal/jawab thoda chhota aur saaf rakho")


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
        "👀 Hii sabko! 💕 Main <b>" + BOT_NAME + "</b> aa gayi is group me.\n\n"
        "💬 Chat ke liye mujhe tag karo ya mere message par reply karo\n"
        "🎁 /daily se roz free coins (DM me)\n"
        "💣 /bomb &lt;amount&gt; se group game shuru karo\n"
        "⚔️ /rob aur /kill se economy ka maza lo\n"
        "Chalo, masti shuru karein! 🎉"
    )
