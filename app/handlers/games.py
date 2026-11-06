"""Group games: /bomb (pass-the-bomb for coins) + /truth /dare /joke.

Bomb design:
- /bomb <amount> escrows the creator's stake to the house row, opens a
  60-second lobby; joiners stake the same amount the same way.
- The bomb sits with a random holder; only the holder can press Pass.
  Each hold has a random 20-60s fuse (an asyncio timer guarded by a
  round token, so an old timer can never explode a new holder).
- Whoever holds it at explosion is out and their stake feeds the pot.
  Last player alive wins the pot (5% house cut is burned).
- All money/state goes through db helpers + transfer(); a restart can
  never corrupt balances — refund_stale_games() refunds unfinished
  games wholesale at startup.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db
from ..db import HOUSE_ID
from .common import mention

log = logging.getLogger("bot.handlers.games")
router = Router()

LOBBY_SECONDS = 60
FUSE_MIN, FUSE_MAX = 20, 60
HOUSE_CUT_PERCENT = 5

_tasks: set = set()
_round_token: dict = {}
_round_msg: dict = {}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _player_name(user_id: int) -> str:
    row = await db.get_user(user_id)
    if row:
        return row.get("first_name") or row.get("username") or "Player"
    return "Player"


async def _lobby_text(game: dict, players: list) -> str:
    names = []
    for p in players:
        names.append("• " + mention(p["user_id"], await _player_name(p["user_id"])))
    pot = int(game["amount"]) * len(players)
    lines = [
        "💣 <b>BOMB GAME — Lobby is open!</b>",
        "🎟 Stake: <b>" + "{:,}".format(int(game["amount"])) + "</b> coins per player",
        "💰 Pot so far: <b>" + "{:,}".format(pot) + "</b> coins",
        "",
        "<b>Players (" + str(len(players)) + "):</b>",
    ] + names + [
        "",
        "Whoever holds the bomb when it explodes is out — the last player standing wins the pot!",
    ]
    return "\n".join(lines)


def _lobby_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Join", callback_data="bomb:join:" + str(game_id)),
         InlineKeyboardButton(text="▶️ Start (host)", callback_data="bomb:start:" + str(game_id))],
        [InlineKeyboardButton(text="❌ Cancel (host)", callback_data="bomb:cancel:" + str(game_id))],
    ])


def _pass_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💣 Pass the bomb!", callback_data="bomb:pass:" + str(game_id))]
    ])


async def _refund_everyone(game: dict) -> None:
    players = await db.get_game_players(game["id"])
    for p in players:
        await db.transfer(HOUSE_ID, p["user_id"], int(game["amount"]), tax_percent=0.0)


async def _cancel_game(bot: Bot, game_id: int, reason: str) -> None:
    game = await db.get_game(game_id)
    if not game or game["status"] not in ("lobby", "running"):
        return
    _round_token[game_id] = _round_token.get(game_id, 0) + 1  # kill any fuse
    await _refund_everyone(game)
    await db.set_game_fields(game_id, status="cancelled", finished_at=datetime.now(timezone.utc))
    try:
        await bot.send_message(game["chat_id"], "🚫 Bomb game cancelled — " + reason + " Everyone's stakes have been refunded.")
    except Exception:
        log.exception("cancel notice failed")


async def refund_stale_games() -> None:
    """Startup cleanup: any lobby/running game older than 10 minutes is
    cancelled with a full refund (its fuse timers died with the process)."""
    for game in await db.stale_games(older_minutes=10):
        try:
            await _refund_everyone(game)
            await db.set_game_fields(
                game["id"], status="cancelled", finished_at=datetime.now(timezone.utc)
            )
            log.info("refunded stale game %s", game["id"])
        except Exception:
            log.exception("stale game refund failed for %s", game.get("id"))


# ------------------------------------------------------------------ lobby --

@router.message(Command("bomb"))
async def cmd_bomb(message: Message, command: CommandObject) -> None:
    if message.chat.type == "private":
        await message.reply("💣 The bomb game is played in a <b>group</b> — add me to a group and send /bomb there!")
        return
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) <= 0:
        await message.reply("💣 Usage: /bomb &lt;amount&gt; — e.g. /bomb 500")
        return
    amount = int(arg)
    creator = message.from_user
    await db.get_or_create_user(creator.id, creator.username, creator.first_name)

    existing = await db.active_game_in_chat(message.chat.id)
    if existing:
        await message.reply("⏳ A bomb game is already running in this group (stake " + "{:,}".format(int(existing["amount"])) + ") — let it finish first!")
        return

    ok, _ = await db.transfer(creator.id, HOUSE_ID, amount, tax_percent=0.0)
    if not ok:
        row = await db.get_user(creator.id)
        bal = int((row or {}).get("coins") or 0)
        await message.reply("😅 Not enough coins — stake is " + "{:,}".format(amount) + ", you have " + "{:,}".format(bal) + ". Earn more with /daily!")
        return

    game_id = await db.create_game(message.chat.id, creator.id, amount)
    await db.add_game_player(game_id, creator.id)
    game = await db.get_game(game_id)
    players = await db.get_game_players(game_id)
    lobby = await message.answer(await _lobby_text(game, players), reply_markup=_lobby_keyboard(game_id))
    await db.set_game_fields(game_id, lobby_message_id=lobby.message_id)
    _spawn(_lobby_timer(message.bot, game_id))


async def _lobby_timer(bot: Bot, game_id: int) -> None:
    await asyncio.sleep(LOBBY_SECONDS)
    game = await db.get_game(game_id)
    if not game or game["status"] != "lobby":
        return
    players = await db.get_game_players(game_id)
    if len(players) < 2:
        await _cancel_game(bot, game_id, "only one player joined, so the game couldn't start.")
        return
    await _start_game(bot, game)


@router.callback_query(F.data.startswith("bomb:join:"))
async def on_join(callback: CallbackQuery) -> None:
    game_id = int(callback.data.rsplit(":", 1)[1])
    game = await db.get_game(game_id)
    user = callback.from_user
    if not game or game["status"] != "lobby":
        await callback.answer("The lobby is already closed 😅", show_alert=True)
        return
    players = await db.get_game_players(game_id)
    if any(p["user_id"] == user.id for p in players):
        await callback.answer("You've already joined 😄")
        return
    await db.get_or_create_user(user.id, user.username, user.first_name)
    ok, _ = await db.transfer(user.id, HOUSE_ID, int(game["amount"]), tax_percent=0.0)
    if not ok:
        await callback.answer("Not enough coins — you need a stake of " + "{:,}".format(int(game["amount"])) + " 😅", show_alert=True)
        return
    await db.add_game_player(game_id, user.id)
    await callback.answer("Joined! 💣 All the best")
    players = await db.get_game_players(game_id)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                await _lobby_text(game, players), reply_markup=_lobby_keyboard(game_id)
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("bomb:start:"))
async def on_start(callback: CallbackQuery) -> None:
    game_id = int(callback.data.rsplit(":", 1)[1])
    game = await db.get_game(game_id)
    if not game or game["status"] != "lobby":
        await callback.answer("The game has already started/finished", show_alert=True)
        return
    if callback.from_user.id != game["creator_id"]:
        await callback.answer("Only the host can start 😄", show_alert=True)
        return
    players = await db.get_game_players(game_id)
    if len(players) < 2:
        await callback.answer("Need at least 2 players!", show_alert=True)
        return
    await callback.answer("Game started! 💣")
    await _start_game(callback.bot, game)


@router.callback_query(F.data.startswith("bomb:cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    game_id = int(callback.data.rsplit(":", 1)[1])
    game = await db.get_game(game_id)
    if not game or game["status"] != "lobby":
        await callback.answer("Can't cancel now", show_alert=True)
        return
    if callback.from_user.id != game["creator_id"]:
        await callback.answer("Only the host can cancel", show_alert=True)
        return
    await callback.answer("Cancelling…")
    await _cancel_game(callback.bot, game_id, "the host cancelled it.")


# ------------------------------------------------------------------ play --

async def _start_game(bot: Bot, game: dict) -> None:
    players = await db.get_game_players(game["id"], alive_only=True)
    if len(players) < 2:
        await _cancel_game(bot, game["id"], "not enough players left.")
        return
    holder = random.choice(players)["user_id"]
    await db.set_game_fields(
        game["id"], status="running", current_holder=holder,
        started_at=datetime.now(timezone.utc),
    )
    if game.get("lobby_message_id"):
        try:
            await bot.edit_message_reply_markup(
                game["chat_id"], game["lobby_message_id"], reply_markup=None
            )
        except Exception:
            pass
    await bot.send_message(
        game["chat_id"],
        "💣💥 <b>GAME STARTED!</b>\nThe bomb is with " + mention(holder, await _player_name(holder))
        + "! Press <b>Pass</b> before it explodes!",
        reply_markup=_pass_keyboard(game["id"]),
    )
    _arm_fuse(bot, game["id"], holder)


def _arm_fuse(bot: Bot, game_id: int, holder_id: int) -> None:
    token = _round_token.get(game_id, 0) + 1
    _round_token[game_id] = token
    fuse = random.uniform(FUSE_MIN, FUSE_MAX)
    _spawn(_fuse(bot, game_id, holder_id, token, fuse))


async def _fuse(bot: Bot, game_id: int, holder_id: int, token: int, seconds: float) -> None:
    await asyncio.sleep(seconds)
    if _round_token.get(game_id) != token:
        return  # bomb already passed on — this fuse is dead
    game = await db.get_game(game_id)
    if not game or game["status"] != "running" or game.get("current_holder") != holder_id:
        return
    await _explode(bot, game, holder_id)


@router.callback_query(F.data.startswith("bomb:pass:"))
async def on_pass(callback: CallbackQuery) -> None:
    game_id = int(callback.data.rsplit(":", 1)[1])
    game = await db.get_game(game_id)
    if not game or game["status"] != "running":
        await callback.answer("The game is over", show_alert=True)
        return
    if callback.from_user.id != game.get("current_holder"):
        await callback.answer("The bomb isn't with you 😄 just wait", show_alert=True)
        return
    alive = await db.get_game_players(game_id, alive_only=True)
    others = [p for p in alive if p["user_id"] != callback.from_user.id]
    if not others:
        await callback.answer("Nobody else is left!")
        return
    next_holder = random.choice(others)["user_id"]
    await db.set_game_fields(game_id, current_holder=next_holder)
    await callback.answer("Passed! 😮‍💨")
    if isinstance(callback.message, Message):
        _round_msg[game_id] = callback.message.message_id
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.bot.send_message(
        game["chat_id"],
        "💣 The bomb is now with " + mention(next_holder, await _player_name(next_holder)) + "! Pass it fast!",
        reply_markup=_pass_keyboard(game_id),
    )
    _arm_fuse(callback.bot, game_id, next_holder)


async def _explode(bot: Bot, game: dict, holder_id: int) -> None:
    game_id = game["id"]
    _round_token[game_id] = _round_token.get(game_id, 0) + 1
    await db.eliminate_player(game_id, holder_id)
    name = await _player_name(holder_id)
    alive = await db.get_game_players(game_id, alive_only=True)
    total_players = await db.get_game_players(game_id)
    pot = int(game["amount"]) * len(total_players)

    if len(alive) <= 1:
        winner = alive[0]["user_id"] if alive else None
        await db.set_game_fields(
            game_id, status="finished", winner_id=winner,
            finished_at=datetime.now(timezone.utc),
        )
        if winner is not None:
            payout = pot * (100 - HOUSE_CUT_PERCENT) // 100
            await db.transfer(HOUSE_ID, winner, payout, tax_percent=0.0)
            wname = await _player_name(winner)
            await bot.send_message(
                game["chat_id"],
                "💥💣 <b>BOOOOM!</b> The bomb exploded in " + mention(holder_id, name) + "'s hands!\n\n"
                "🏆 " + mention(winner, wname) + " is the last one standing — won <b>" + "{:,}".format(payout)
                + "</b> coins! (pot " + "{:,}".format(pot) + ", 5% house cut)",
            )
        return

    next_holder = random.choice(alive)["user_id"]
    await db.set_game_fields(game_id, current_holder=next_holder)
    await bot.send_message(
        game["chat_id"],
        "💥💣 <b>BOOOOM!</b> " + mention(holder_id, name) + " is out! Their stake went into the pot.\n"
        "Players left: <b>" + str(len(alive)) + "</b>\n\n"
        "💣 The bomb is now with " + mention(next_holder, await _player_name(next_holder)) + "!",
        reply_markup=_pass_keyboard(game_id),
    )
    _arm_fuse(bot, game_id, next_holder)


# -------------------------------------------------------------- fun stuff --

TRUTHS = [
    "Tumne last jhooth kab aur kisko bola tha?",
    "Phone me sabse embarrassing photo kaunsi hai — describe karo, dikhao mat 😂",
    "Crush ka naam batao… sirf pehla letter hi chalega 😏",
    "Sabse bada dar kya hai tumhara?",
    "Bachpan ki sabse sharminda kar dene wali harkat?",
    "Agar ek din ke liye invisible ho jao, toh pehla kaam kya karoge?",
    "Kisi ko stalk kiya hai kabhi social media par? Sach bolo 👀",
    "Sabse bekaar kharida hua cheez kaunsi thi jiska abhi bhi afsos hai?",
    "Apni ek aisi aadat batao jo tum chhupate ho sabse",
    "Last time roye kab the aur kyun?",
    "Tumhare phone ka screen time sach me kitna hai?",
    "Agar best friend ka phone ek ghante ke liye mil jaye toh kya check karoge pehle?",
    "Worst date ya worst chat experience batao (naam mat lena)",
    "Ek cheez jo tum parents se abhi tak chhupate ho?",
]

DARES = [
    "Apni profile photo par ek funny caption lagao aur 10 minute rehne do 😂",
    "Apni voice me ek filmy dialogue bolkar group me voice note bhejo 🎙",
    "Agla message sirf emojis me likho, ek shabd nahi",
    "Group ke kisi ek bande ko filmy style me propose karo (mazaak me!) 💍",
    "Apne naam ka ulta spelling likh kar bhejo, bina soche",
    "Ek minute ke liye har message ke end me 'meow' likho 🐱",
    "Koi bhi ek gaana gunguna kar voice note bhejo 🎵",
    "Apni gallery ki 5th photo ka sirf description do — photo nahi bhejni",
    "Group ke sab logo ko ek ek line me roast karo, pyaar se 😏",
    "Agli 3 messages me khud ko teesre bande ki tarah refer karo ('woh kehta hai…')",
    "Apna sabse purana yaadgar school wala किस्सा ek line me sunao",
    "Ek tongue twister 3 baar bina ruke voice note me bolo",
    "Apne status me abhi 'Bomb game champion 💣' lagao",
]

JOKES = [
    "Teacher: Homework kyun nahi kiya? Student: Sir, light gayi thi. Teacher: Toh mombatti jala lete. Student: Sir, lighter bhi battery par chalta hai 😂",
    "Pappu: Doctor sahab, mujhe bhoolne ki bimari hai. Doctor: Kab se hai? Pappu: Kab se kya hai? 🤔",
    "Biwi: Sunte ho, padosan ka pati roz use ghumane le jata hai. Pati: Maine bhi bola tha use, woh maani hi nahi 😅",
    "Ek aadmi library me gaya aur zor se bola: 'Ek burger aur fries!' Librarian: Ye library hai. Aadmi (dheere se): 'Ek burger aur fries…' 🤫",
    "Santa: Yaar, meri biwi khana bahut acha banati hai. Banta: Tumhe kaise pata? Santa: Uske banane ke baad khana bach hi nahi raha, sab dustbin me! 😂",
    "Teacher: 'Kal' ka sentence banao. Student: Main kal se padhai shuru karunga. Teacher: Ye kal kab aayega? Student: Wahi toh, kal kabhi aata hi nahi 😌",
    "Mobile ne insaan ko itna badal diya hai — ab log khana khane se pehle khane ki photo kha lete hain 📸",
    "Girlfriend: Baby, tum mujhe kitna pyaar karte ho? Boyfriend: Jio ke recharge jitna — har mahine renew karna padta hai 😂",
    "Judge: Tumne chori kyun ki? Chor: Sahab, bhook lagi thi. Judge: Toh jail me khana milega. Chor: Tab toh pakka chori karunga 😅",
    "Dost: Yaar wifi ka password kya hai? Main: 'passwordgalathai' Dost: Kaam nahi kar raha! Main: Maine toh pehle hi bola tha 😏",
    "Ek machhar aadmi se: Bhai khoon peene do. Aadmi: Kyun? Machhar: Net par dekha tha, donation karne se punya milta hai 🦟",
    "Beta: Papa, main bada hokar pilot banoonga. Papa: Aur plane kaun udayega? Beta: Co-pilot hota hai na kaam ke liye 😎",
    "Boss: Tum hamesha late kyun aate ho? Employee: Sir, traffic. Boss: Kal se jaldi niklo. Employee: Nikalta toh jaldi hu, office hi late khulta hai 😂",
]


@router.message(Command("truth"))
async def cmd_truth(message: Message) -> None:
    await message.reply("😜 <b>Truth:</b> " + random.choice(TRUTHS))


@router.message(Command("dare"))
async def cmd_dare(message: Message) -> None:
    await message.reply("😈 <b>Dare:</b> " + random.choice(DARES))


@router.message(Command("joke"))
async def cmd_joke(message: Message) -> None:
    await message.reply("😂 " + random.choice(JOKES))
