"""Entrypoint — FastAPI + aiogram webhook server (port 7860, HF Spaces).

Concurrency design (the user's hard requirement):
- The webhook route does almost nothing: verify the secret, validate the
  update, dedup by update_id, drop it in an asyncio.Queue, return 200.
  Telegram never waits on our processing, and 100 groups tagging at once
  just means a deeper queue — nothing blocks anything else.
- A pool of worker tasks drains the queue and feeds updates to the
  dispatcher. Inside handlers, a per-chat lock keeps one chat's
  conversation in order while different chats run fully in parallel,
  and the LLM layer caps global parallel model calls with a semaphore
  (LLM_MAX_PARALLEL) so the free-tier quota is never melted.
- POLLING=1 switches to long polling for local development (no webhook).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import admin as admin_panel
from . import db, harvest, pairs
from .config import get_settings
from .handlers import chat, common, economy, games, own

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot.main")

settings = get_settings()

bot: Bot | None = None
dp = Dispatcher()
# Order matters: command routers first, free-chat router LAST (its text
# handler would otherwise swallow commands before they are routed).
dp.include_router(common.router)
dp.include_router(economy.router)
dp.include_router(games.router)
dp.include_router(own.router)
dp.include_router(chat.router)
# Silent group harvesting observes every fresh message BEFORE handlers
# run and never consumes the update (see app/harvest.py).
dp.message.outer_middleware(harvest.HarvestMiddleware())

_queue: asyncio.Queue = asyncio.Queue()
_seen_updates: set = set()
_workers: list = []
_polling_task = None
_maintenance_task = None

WORKER_COUNT = 8


async def _worker() -> None:
    while True:
        update = await _queue.get()
        try:
            await dp.feed_update(bot, update)
        except Exception:
            log.exception("update processing failed (id=%s)", getattr(update, "update_id", "?"))
        finally:
            _queue.task_done()


async def _maintenance_loop() -> None:
    """Hourly housekeeping: prune raw chat history older than 24h
    (summaries and learned pairs are permanent — db.cleanup_old_messages
    never touches them)."""
    while True:
        await asyncio.sleep(3600)
        try:
            await db.cleanup_old_messages()
        except Exception:
            log.exception("periodic chat-history cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, _polling_task, _maintenance_task
    missing = settings.missing_for_run()
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await db.init_pool()
    await db.init_schema()
    log.info(
        "admin panel %s",
        "enabled at /admin" if settings.admin_panel_key else "disabled (ADMIN_PANEL_KEY unset)",
    )
    await pairs.ensure_seeds()
    await games.refund_stale_games()
    await db.cleanup_old_messages()
    _maintenance_task = asyncio.create_task(_maintenance_loop())

    if settings.polling:
        await bot.delete_webhook(drop_pending_updates=True)
        _polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("POLLING mode — webhook skipped")
    else:
        await bot.set_webhook(
            url=settings.webhook_base_url + "/webhook",
            secret_token=settings.webhook_secret or None,
            drop_pending_updates=True,
        )
        for _ in range(WORKER_COUNT):
            _workers.append(asyncio.create_task(_worker()))
        log.info("webhook set to %s/webhook, %d workers running", settings.webhook_base_url, WORKER_COUNT)

    try:
        yield
    finally:
        for task in _workers:
            task.cancel()
        if _polling_task is not None:
            _polling_task.cancel()
        if _maintenance_task is not None:
            _maintenance_task.cancel()
        await db.close_pool()
        if bot is not None:
            await bot.session.close()


app = FastAPI(lifespan=lifespan)
# Admin dashboard — every /admin route 404s while ADMIN_PANEL_KEY is unset.
app.include_router(admin_panel.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    if settings.webhook_secret:
        got = request.headers.get("x-telegram-bot-api-secret-token")
        if got != settings.webhook_secret:
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
    except Exception:
        log.exception("could not parse update")
        return JSONResponse({"ok": False}, status_code=400)
    if update.update_id in _seen_updates:
        return {"ok": True}
    _seen_updates.add(update.update_id)
    if len(_seen_updates) > 20000:
        _seen_updates.clear()
    _queue.put_nowait(update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port)
