"""Postgres data layer (asyncpg -> Supabase).

Rules of the house:
- Every coin movement between two users goes through transfer(), which locks
  both rows in user_id order (deadlock-safe) inside one transaction.
- Rewards that mint coins (daily, kill) and burns are separate small helpers,
  each also a single transaction.
- Game stakes are escrowed to the house row (user_id = 0).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from .config import get_settings

log = logging.getLogger("bot.db")

HOUSE_ID = 0
# Short-term chat history lives 24 hours; beyond that, a conversation is
# represented only by its rolling summary. Learned pairs and summaries
# themselves are permanent and are never pruned.
HISTORY_RETENTION_HOURS = 24
_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=10, init=_init_conn
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def init_schema() -> None:
    """Create all tables if missing (schema.sql sits next to the app package)."""
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / "schema.sql", here.parent / "schema.sql"]
    sql = None
    for path in candidates:
        if path.exists():
            sql = path.read_text(encoding="utf-8")
            break
    if sql is None:
        raise RuntimeError("schema.sql not found next to the app package")
    async with pool().acquire() as conn:
        await conn.execute(sql)
    log.info("DB schema ready")


# ---------------------------------------------------------------- users ----

async def _ensure_user_conn(conn, user_id: int, username=None, first_name=None) -> None:
    await conn.execute(
        "INSERT INTO users (user_id, username, first_name) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO NOTHING",
        user_id, username, first_name,
    )


async def get_or_create_user(user_id: int, username=None, first_name=None) -> dict:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "username = COALESCE(EXCLUDED.username, users.username), "
            "first_name = COALESCE(EXCLUDED.first_name, users.first_name)",
            user_id, username, first_name,
        )
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return dict(row)


async def get_user(user_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict | None:
    username = (username or "").lstrip("@").strip()
    if not username:
        return None
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE lower(username) = lower($1)", username
        )
    return dict(row) if row else None


async def set_user_gender(user_id: int, gender: str) -> None:
    """Persist a gender detected by app.gender.detect_gender.

    Callers only ever pass a fresh confident detection, so a stored
    opposite value is overwritten (a misdetection stays correctable),
    while None is never written — an unknown message can never clear
    a known gender.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET gender = $2 WHERE user_id = $1 "
            "AND gender IS DISTINCT FROM $2",
            user_id, gender,
        )


async def get_rank(user_id: int) -> int:
    async with pool().acquire() as conn:
        rank = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM users WHERE coins > "
            "(SELECT coins FROM users WHERE user_id = $1)",
            user_id,
        )
    return int(rank or 1)


async def top_rich(limit: int = 10) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE user_id <> 0 ORDER BY coins DESC LIMIT $1", limit
        )
    return [dict(r) for r in rows]


def is_protected(user: dict) -> bool:
    until = user.get("protected_until")
    return bool(until and until > datetime.now(timezone.utc))


# ------------------------------------------------------------- transfers ----

async def transfer(from_id: int, to_id: int, amount: int, tax_percent: float = 0.0):
    """Move `amount` coins from one user to another, atomically.

    The tax is burned (removed from circulation); the receiver gets
    amount - tax. Returns (success, credited_amount). Both rows are locked
    in ascending user_id order so concurrent opposite transfers cannot
    deadlock.
    """
    if amount <= 0 or from_id == to_id:
        return (False, 0)
    tax = int(amount * tax_percent / 100.0)
    credited = amount - tax
    ok = False
    async with pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user_conn(conn, from_id)
            await _ensure_user_conn(conn, to_id)
            rows = await conn.fetch(
                "SELECT user_id, coins FROM users WHERE user_id = ANY($1::bigint[]) "
                "ORDER BY user_id FOR UPDATE",
                [from_id, to_id],
            )
            balances = {r["user_id"]: r["coins"] for r in rows}
            if balances.get(from_id, 0) >= amount:
                await conn.execute(
                    "UPDATE users SET coins = coins - $2 WHERE user_id = $1", from_id, amount
                )
                await conn.execute(
                    "UPDATE users SET coins = coins + $2 WHERE user_id = $1", to_id, credited
                )
                ok = True
    return (ok, credited if ok else 0)


async def add_coins(user_id: int, amount: int) -> None:
    """Mint coins for a user (rewards only — daily, kill bonus)."""
    if amount <= 0:
        return
    async with pool().acquire() as conn:
        await _ensure_user_conn(conn, user_id)
        await conn.execute(
            "UPDATE users SET coins = coins + $2 WHERE user_id = $1", user_id, amount
        )


async def try_claim_daily(user_id: int, coins_reward: int, xp_reward: int) -> bool:
    """Atomically claim the 24h daily reward. True if it was granted."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET coins = coins + $2, xp = xp + $3, last_daily = now() "
            "WHERE user_id = $1 AND "
            "(last_daily IS NULL OR last_daily <= now() - INTERVAL '24 hours') "
            "RETURNING user_id",
            user_id, coins_reward, xp_reward,
        )
    return row is not None


async def buy_protection(user_id: int, days: int, cost: int) -> bool:
    """Deduct `cost` and extend protection by `days` from the later of
    now / current expiry. Single transaction."""
    now = datetime.now(timezone.utc)
    ok = False
    async with pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user_conn(conn, user_id)
            row = await conn.fetchrow(
                "SELECT coins, protected_until FROM users WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if row and row["coins"] >= cost:
                base = row["protected_until"] if row["protected_until"] and row["protected_until"] > now else now
                new_until = base + timedelta(days=days)
                await conn.execute(
                    "UPDATE users SET coins = coins - $2, protected_until = $3 WHERE user_id = $1",
                    user_id, cost, new_until,
                )
                ok = True
    return ok


async def kill_action(killer_id: int, target_id: int, reward: int, loss_percent: float) -> int:
    """One kill, one transaction: target loses loss_percent% of coins (burned),
    killer gains the fixed reward and +1 kill. Returns the amount burned."""
    loss = 0
    async with pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_user_conn(conn, killer_id)
            await _ensure_user_conn(conn, target_id)
            rows = await conn.fetch(
                "SELECT user_id, coins FROM users WHERE user_id = ANY($1::bigint[]) "
                "ORDER BY user_id FOR UPDATE",
                [killer_id, target_id],
            )
            balances = {r["user_id"]: r["coins"] for r in rows}
            loss = int(balances.get(target_id, 0) * loss_percent / 100.0)
            if loss > 0:
                await conn.execute(
                    "UPDATE users SET coins = coins - $2 WHERE user_id = $1", target_id, loss
                )
            await conn.execute(
                "UPDATE users SET coins = coins + $2, kills = kills + 1 WHERE user_id = $1",
                killer_id, reward,
            )
    return loss


# ----------------------------------------------------------------- chats ----

def _as_msg_list(raw) -> list:
    """Normalise a stored `chats.messages` value into a list of dicts.

    Rows written before the jsonb-codec fix were double-encoded — the
    codec decoded them back to a plain string holding the JSON array
    instead of a list. Decode strings (repeatedly, in case of multiple
    encodings), then keep only dict entries, so readers never crash on
    a legacy string entry.
    """
    value = raw
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [m for m in value if isinstance(m, dict)]


async def get_history(chat_id: int, user_id: int):
    """Returns (summary, last messages) for one (chat, user) pair.

    Only messages from the last HISTORY_RETENTION_HOURS are returned as
    context — anything older reaches the LLM only through the rolling
    summary. Entries stored before timestamps existed count as expired.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, messages FROM chats WHERE chat_id = $1 AND user_id = $2",
            chat_id, user_id,
        )
    if not row:
        return ("", [])
    cutoff = datetime.now(timezone.utc).timestamp() - HISTORY_RETENTION_HOURS * 3600
    fresh = [
        m for m in _as_msg_list(row["messages"])
        if float(m.get("ts") or 0) >= cutoff
    ]
    return (row["summary"] or "", fresh)


async def append_message(chat_id: int, user_id: int, role: str, text: str, keep: int = 12) -> list:
    """Append one message, trim to the last `keep`, return any dropped ones
    (the caller can fold them into the rolling summary)."""
    dropped: list = []
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT messages FROM chats WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, user_id,
            )
            msgs = _as_msg_list(row["messages"]) if row else []
            msgs.append({
                "role": role,
                "text": text,
                "ts": int(datetime.now(timezone.utc).timestamp()),
            })
            if len(msgs) > keep:
                dropped = msgs[:-keep]
                msgs = msgs[-keep:]
            await conn.execute(
                "INSERT INTO chats (chat_id, user_id, messages, updated_at) "
                "VALUES ($1, $2, $3::jsonb, now()) "
                "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
                "messages = EXCLUDED.messages, updated_at = now()",
                chat_id, user_id, msgs,
            )
    return dropped


async def update_summary(chat_id: int, user_id: int, summary: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO chats (chat_id, user_id, summary, updated_at) VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET summary = EXCLUDED.summary, updated_at = now()",
            chat_id, user_id, summary,
        )


async def cleanup_old_messages(hours: int = HISTORY_RETENTION_HOURS) -> int:
    """Prune stored raw chat messages older than `hours` from chats rows.

    Only the raw short-term history expires — summaries and learned pairs
    are permanent and are never touched here. Runs at startup and hourly
    from main.py's maintenance loop. Returns entries pruned.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    pruned = 0
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, user_id, messages FROM chats")
        for row in rows:
            msgs = _as_msg_list(row["messages"])
            fresh = [m for m in msgs if float(m.get("ts") or 0) >= cutoff]
            if len(fresh) < len(msgs):
                pruned += len(msgs) - len(fresh)
                await conn.execute(
                    "UPDATE chats SET messages = $3::jsonb WHERE chat_id = $1 AND user_id = $2",
                    row["chat_id"], row["user_id"], fresh,
                )
    if pruned:
        log.info("pruned %d stored chat messages older than %dh", pruned, hours)
    return pruned


# -------------------------------------------------------------- stickers ----

async def add_sticker(file_id: str, mood: str, added_by: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO stickers (file_id, mood, added_by) VALUES ($1, $2, $3) "
            "ON CONFLICT (file_id) DO UPDATE SET mood = EXCLUDED.mood",
            file_id, mood, added_by,
        )


async def random_sticker(mood: str):
    async with pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT file_id FROM stickers WHERE mood = $1 ORDER BY random() LIMIT 1", mood
        )


# ----------------------------------------------------------------- packs ----

async def get_user_packs(user_id: int) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM user_packs WHERE user_id = $1 ORDER BY pack_index", user_id
        )
    return [dict(r) for r in rows]


async def get_pack(user_id: int, pack_index: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_packs WHERE user_id = $1 AND pack_index = $2",
            user_id, pack_index,
        )
    return dict(row) if row else None


async def create_pack_record(user_id: int, pack_index: int, pack_name: str, title: str, fmt: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO user_packs (user_id, pack_index, pack_name, title, format) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, pack_index, pack_name, title, fmt,
        )


# ----------------------------------------------------------------- games ----

async def create_game(chat_id: int, creator_id: int, amount: int) -> int:
    async with pool().acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO games (chat_id, creator_id, amount) VALUES ($1, $2, $3) RETURNING id",
            chat_id, creator_id, amount,
        )


async def get_game(game_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM games WHERE id = $1", game_id)
    return dict(row) if row else None


async def set_game_fields(game_id: int, **fields) -> None:
    """Update whitelisted game columns."""
    allowed = {"status", "current_holder", "winner_id", "lobby_message_id", "started_at", "finished_at"}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    sets = ", ".join(k + " = $" + str(i + 2) for i, (k, _v) in enumerate(items))
    values = [v for _k, v in items]
    async with pool().acquire() as conn:
        await conn.execute("UPDATE games SET " + sets + " WHERE id = $1", game_id, *values)


async def add_game_player(game_id: int, user_id: int) -> bool:
    async with pool().acquire() as conn:
        val = await conn.fetchval(
            "INSERT INTO game_players (game_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT (game_id, user_id) DO NOTHING RETURNING 1",
            game_id, user_id,
        )
    return val is not None


async def get_game_players(game_id: int, alive_only: bool = False) -> list:
    sql = "SELECT * FROM game_players WHERE game_id = $1"
    if alive_only:
        sql += " AND alive = TRUE"
    sql += " ORDER BY joined_at"
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, game_id)
    return [dict(r) for r in rows]


async def eliminate_player(game_id: int, user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE game_players SET alive = FALSE WHERE game_id = $1 AND user_id = $2",
            game_id, user_id,
        )


async def active_game_in_chat(chat_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM games WHERE chat_id = $1 AND status IN ('lobby', 'running') "
            "ORDER BY id DESC LIMIT 1",
            chat_id,
        )
    return dict(row) if row else None


async def stale_games(older_minutes: int = 10) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM games WHERE status IN ('lobby', 'running') "
            "AND created_at < now() - make_interval(mins => $1)",
            older_minutes,
        )
    return [dict(r) for r in rows]


# -------------------------------------------------- harvested stickers ----

async def record_harvested_sticker(scope: str, kind: str, stimulus_key: str,
                                   file_id: str, stimulus_raw: str | None = None) -> None:
    """Record one observed sticker association (see app/harvest.py).

    kind='text'    -> stimulus_key is normalised text the sticker replied to.
    kind='sticker' -> stimulus_key is the file_id of the sticker replied to.
    Re-observing the same association just bumps its hit counter.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO harvested_stickers (scope, kind, stimulus_key, stimulus_raw, file_id) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (scope, kind, stimulus_key, file_id) "
            "DO UPDATE SET hits = harvested_stickers.hits + 1, updated_at = now()",
            scope, kind, stimulus_key, stimulus_raw, file_id,
        )


async def harvested_stimulus_keys(scope: str, kind: str = "text", cap: int = 5000) -> list:
    """Distinct stimulus keys in a scope, most-observed first (capped)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT stimulus_key FROM harvested_stickers WHERE scope = $1 AND kind = $2 "
            "GROUP BY stimulus_key ORDER BY SUM(hits) DESC LIMIT $3",
            scope, kind, cap,
        )
    return [r["stimulus_key"] for r in rows]


async def harvested_responses(scope: str, kind: str, stimulus_key: str) -> list:
    """Sticker file_ids associated with one stimulus, most-observed first."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT file_id, hits FROM harvested_stickers "
            "WHERE scope = $1 AND kind = $2 AND stimulus_key = $3 "
            "ORDER BY hits DESC LIMIT 10",
            scope, kind, stimulus_key,
        )
    return [dict(r) for r in rows]


# ------------------------------------------------------------ admin reads ----

async def admin_stats() -> dict:
    """Aggregate counts for the /admin overview page."""
    async with pool().acquire() as conn:
        out = {}
        out["users"] = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id <> 0")
        out["chat_rows"] = await conn.fetchval("SELECT COUNT(*) FROM chats")
        out["chats_seen"] = await conn.fetchval("SELECT COUNT(DISTINCT chat_id) FROM chats")
        out["groups_seen"] = await conn.fetchval(
            "SELECT COUNT(DISTINCT chat_id) FROM chats WHERE chat_id < 0"
        )
        out["pairs_total"] = await conn.fetchval("SELECT COUNT(*) FROM learned_pairs")
        out["pairs_hits"] = await conn.fetchval(
            "SELECT COALESCE(SUM(hits), 0) FROM learned_pairs"
        )
        rows = await conn.fetch(
            "SELECT source, COUNT(*) AS n FROM learned_pairs GROUP BY source"
        )
        out["pairs_by_source"] = {r["source"]: r["n"] for r in rows}
        out["sticker_pool"] = await conn.fetchval("SELECT COUNT(*) FROM stickers")
        out["harvested_total"] = await conn.fetchval("SELECT COUNT(*) FROM harvested_stickers")
        out["harvested_hits"] = await conn.fetchval(
            "SELECT COALESCE(SUM(hits), 0) FROM harvested_stickers"
        )
        out["games_total"] = await conn.fetchval("SELECT COUNT(*) FROM games")
        out["games_finished"] = await conn.fetchval(
            "SELECT COUNT(*) FROM games WHERE status = 'finished'"
        )
    return out


async def list_pairs(q: str = "", scope: str = "", source: str = "",
                     limit: int = 25, offset: int = 0):
    """(rows, total) for the /admin pairs browser."""
    where = []
    args: list = []
    if q:
        args.append("%" + q + "%")
        where.append(
            "(stimulus_norm ILIKE $%d OR stimulus_raw ILIKE $%d OR response ILIKE $%d)"
            % (len(args), len(args), len(args))
        )
    if scope:
        args.append(scope)
        where.append("scope = $%d" % len(args))
    if source:
        args.append(source)
        where.append("source = $%d" % len(args))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    async with pool().acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM learned_pairs" + clause, *args)
        rows = await conn.fetch(
            "SELECT id, scope, stimulus_norm, stimulus_raw, response, mood, source, "
            "hits, created_by, created_at FROM learned_pairs" + clause +
            " ORDER BY id DESC LIMIT $%d OFFSET $%d" % (len(args) + 1, len(args) + 2),
            *args, limit, offset,
        )
    return [dict(r) for r in rows], int(total or 0)


async def delete_pair(pair_id: int) -> bool:
    async with pool().acquire() as conn:
        status = await conn.execute("DELETE FROM learned_pairs WHERE id = $1", pair_id)
    return status.endswith(" 1")


async def recent_users(limit: int = 25, offset: int = 0):
    """(rows, total) for the /admin users page, newest first."""
    async with pool().acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id <> 0")
        rows = await conn.fetch(
            "SELECT user_id, username, first_name, coins, gems, xp, kills, created_at "
            "FROM users WHERE user_id <> 0 ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
    return [dict(r) for r in rows], int(total or 0)


async def sticker_pool_rows() -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT file_id, mood, added_by, created_at FROM stickers "
            "ORDER BY mood, created_at"
        )
    return [dict(r) for r in rows]


async def top_harvested_stickers(limit: int = 50) -> list:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope, kind, stimulus_key, stimulus_raw, file_id, hits, updated_at "
            "FROM harvested_stickers ORDER BY hits DESC, updated_at DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]
