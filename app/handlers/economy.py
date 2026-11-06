"""Economy commands (Baka-style): /bal /daily /rob /kill /protect /give /toprich.

All balance changes go through db.transfer() / db helpers — transactions
with ordered row locks — never through read-modify-write in handler code.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db
from .common import mention, stats_card

log = logging.getLogger("bot.handlers.economy")
router = Router()

DAILY_COINS = 2000
DAILY_XP = 50
ROB_TAX = 10.0
GIVE_TAX = 10.0
ROB_CAP = 10_000
KILL_LOSS_PERCENT = 5.0
PROTECT_COSTS = {1: 400, 2: 1000}


async def _self_row(message: Message) -> dict:
    u = message.from_user
    return await db.get_or_create_user(u.id, u.username, u.first_name)


async def _resolve_target(message: Message, args: str):
    """Resolve a command target to (user_id, display_name) or None.

    Order: reply-to message > @username > numeric id.
    """
    rt = message.reply_to_message
    if rt is not None and rt.from_user is not None and not rt.from_user.is_bot:
        return (rt.from_user.id, rt.from_user.first_name or "User")
    tokens = (args or "").split()
    if not tokens:
        return None
    token = tokens[0]
    if token.startswith("@"):
        row = await db.get_user_by_username(token)
        if row:
            return (row["user_id"], row.get("first_name") or token)
        return None
    if token.lstrip("-").isdigit():
        uid = int(token)
        row = await db.get_user(uid)
        return (uid, (row or {}).get("first_name") or str(uid))
    return None


@router.message(Command("bal"))
async def cmd_bal(message: Message) -> None:
    target = await _resolve_target(message, "")
    if target and target[0] != message.from_user.id:
        row = await db.get_or_create_user(target[0], None, target[1])
        await message.reply("👤 " + mention(target[0], target[1]) + "'s stats:\n" + stats_card(row))
        return
    row = await _self_row(message)
    rank = await db.get_rank(row["user_id"])
    await message.reply("👤 <b>Your stats:</b>\n" + stats_card(row) + "\n🏆 Rank: <b>#" + str(rank) + "</b>")


@router.message(Command("daily"))
async def cmd_daily(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        me = await bot.get_me()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Get daily in DM",
                                  url="https://t.me/" + me.username + "?start=daily")]
        ])
        await message.reply("🎁 The daily reward is in <b>DM</b> — send /daily there!", reply_markup=kb)
        return
    row = await _self_row(message)
    if await db.try_claim_daily(row["user_id"], DAILY_COINS, DAILY_XP):
        fresh = await db.get_user(row["user_id"])
        await message.answer(
            "🎁 Daily claimed! <b>+" + str(DAILY_COINS) + " coins</b>, +" + str(DAILY_XP) + " XP\n"
            "💰 Balance: <b>" + "{:,}".format(int(fresh["coins"])) + "</b>"
        )
    else:
        last = row.get("last_daily")
        wait = ""
        if last is not None:
            remaining = last + timedelta(hours=24)
            from datetime import datetime, timezone
            delta = remaining - datetime.now(timezone.utc)
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            wait = " (" + str(hours) + "h " + str(mins) + "m left)"
        await message.answer("⏳ You've already claimed today's daily! Come back tomorrow" + wait)


@router.message(Command("rob"))
async def cmd_rob(message: Message, command: CommandObject) -> None:
    robber = await _self_row(message)
    target = await _resolve_target(message, command.args or "")
    if target is None:
        await message.reply("⚔️ Reply to someone's message with /rob, or use /rob @username")
        return
    target_id, target_name = target
    if target_id == robber["user_id"]:
        await message.reply("Rob yourself? 😂 Try something else")
        return
    victim = await db.get_or_create_user(target_id, None, target_name)
    if db.is_protected(victim):
        await message.reply("🛡 " + mention(target_id, target_name) + " is protected — the rob failed!")
        return
    if int(victim["coins"]) < 100:
        await message.reply("😅 " + mention(target_id, target_name) + " doesn't even have 100 coins — let them be")
        return
    if random.random() < 0.6:
        steal = min(ROB_CAP, max(50, int(int(victim["coins"]) * random.uniform(0.05, 0.15))))
        ok, credited = await db.transfer(target_id, robber["user_id"], steal, tax_percent=ROB_TAX)
        if ok:
            await message.reply(
                "💰 Rob successful! You looted <b>" + "{:,}".format(steal)
                + "</b> coins from " + mention(target_id, target_name)
                + ", after tax you got <b>" + "{:,}".format(credited) + "</b> ⚔️"
            )
        else:
            await message.reply("The rob got stuck midway 😬 Try again")
    else:
        fine = min(int(robber["coins"]), 100)
        if fine > 0:
            await db.transfer(robber["user_id"], target_id, fine, tax_percent=0.0)
        await message.reply(
            "🚨 Caught! Rob failed — " + "{:,}".format(fine) + " coins fine went to " +
            mention(target_id, target_name) + " 😭"
        )


@router.message(Command("kill"))
async def cmd_kill(message: Message, command: CommandObject) -> None:
    killer = await _self_row(message)
    target = await _resolve_target(message, command.args or "")
    if target is None:
        await message.reply("🔪 Reply to someone's message with /kill, or use /kill @username")
        return
    target_id, target_name = target
    if target_id == killer["user_id"]:
        await message.reply("Yourself? 😂 Not happening")
        return
    victim = await db.get_or_create_user(target_id, None, target_name)
    if db.is_protected(victim):
        await message.reply("🛡 " + mention(target_id, target_name) + " is protected — the kill was blocked!")
        return
    reward = random.randint(100, 200)
    loss = await db.kill_action(killer["user_id"], target_id, reward, KILL_LOSS_PERCENT)
    fresh = await db.get_user(killer["user_id"])
    await message.reply(
        "🔪💥 " + mention(target_id, target_name) + " got killed!\n"
        "You earned <b>+" + str(reward) + " coins</b>, and <b>" + "{:,}".format(loss) + "</b> of their coins burned.\n"
        "Total kills: <b>" + str(fresh["kills"]) + "</b>"
    )


@router.message(Command("protect"))
async def cmd_protect(message: Message, command: CommandObject) -> None:
    row = await _self_row(message)
    arg = (command.args or "").strip().lower().rstrip("d")
    days = int(arg) if arg.isdigit() else 0
    if days not in PROTECT_COSTS:
        await message.reply("🛡 Usage: /protect 1d (400 coins) or /protect 2d (1000 coins)")
        return
    cost = PROTECT_COSTS[days]
    if await db.buy_protection(row["user_id"], days, cost):
        await message.reply(
            "🛡 Protection ON! For <b>" + str(days) + (" day" if days == 1 else " days")
            + "</b> nobody can rob/kill you. (" + str(cost) + " coins charged)"
        )
    else:
        await message.reply(
            "😅 Not enough coins — you need " + str(cost) + ", you have " +
            "{:,}".format(int(row["coins"])) + ". Earn more with /daily!"
        )


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject) -> None:
    giver = await _self_row(message)
    args = (command.args or "").split()
    target = None
    amount = None
    rt = message.reply_to_message
    if rt is not None and rt.from_user is not None and not rt.from_user.is_bot and args and args[0].isdigit():
        target = (rt.from_user.id, rt.from_user.first_name or "User")
        amount = int(args[0])
    elif len(args) >= 2 and args[1].isdigit():
        target = await _resolve_target(message, args[0])
        amount = int(args[1])
    if target is None or amount is None:
        await message.reply("🎁 Usage: reply to someone with /give &lt;amount&gt;, or /give @username &lt;amount&gt;")
        return
    target_id, target_name = target
    if target_id == giver["user_id"]:
        await message.reply("Giving to yourself? What's the point 😂")
        return
    if amount <= 0:
        await message.reply("Give a proper amount 😅")
        return
    await db.get_or_create_user(target_id, None, target_name)
    ok, credited = await db.transfer(giver["user_id"], target_id, amount, tax_percent=GIVE_TAX)
    if ok:
        tax = amount - credited
        await message.reply(
            "🎁 " + mention(target_id, target_name) + " received <b>" + "{:,}".format(credited) +
            "</b> coins (tax " + "{:,}".format(tax) + " deducted)"
        )
    else:
        await message.reply(
            "😅 Balance too low — you have " + "{:,}".format(int(giver["coins"])) + " coins"
        )


@router.message(Command("toprich"))
async def cmd_toprich(message: Message) -> None:
    rows = await db.top_rich(10)
    if not rows:
        await message.reply("Nobody's rich yet 😅 Start with /daily!")
        return
    lines = ["🏆 <b>Top 10 Richest</b>\n"]
    for i, r in enumerate(rows, start=1):
        name = r.get("first_name") or r.get("username") or "User"
        lines.append(
            str(i) + ". " + mention(r["user_id"], name) + " — <b>" +
            "{:,}".format(int(r["coins"])) + "</b> coins"
        )
    await message.reply("\n".join(lines))
