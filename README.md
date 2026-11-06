# Misha — Friendly Telegram Chat Bot

A Hinglish girl-persona Telegram bot (Baka-style economy + games, Nezuko-style
chat) with a twist the big bots don't have: a **DB-trained pairs layer**.

Chat flow per message: **learned pair (DB, free) → LLM (only on miss)
→ the new exchange is learned back**. Most daily small talk is answered from
Postgres with zero API calls, so the free-tier daily request quota stretches
much further. Plus: economy (/daily /bal /rob /kill /protect /give /toprich),
a /bomb group game, user sticker packs (/own), mood stickers, and (planned,
currently OFF) voice-note replies.

Stack: Python 3.11 · aiogram 3 · FastAPI webhook · asyncpg → Supabase Postgres ·
NVIDIA / OpenRouter / Groq chat APIs. Concurrency: queue + worker pool, per-chat locks, global LLM
semaphore — built for many groups tagging at once.

## LLM provider rotation

Chat runs on your own API keys across providers, tried in
`LLM_PROVIDER_ORDER` (default `nvidia,openrouter,groq`): NVIDIA →
OpenRouter → Groq. A provider that returns 429 / quota is parked in a cooldown
(`LLM_COOLDOWN_MINUTES`, default 60) and the chain moves on; if every
provider is down or cooling, the bot answers with canned Hinglish lines
instead of crashing. Pairs-layer hits never call any provider.

## Env vars (names only — set real values in your host's secrets)

`BOT_TOKEN`, `WEBHOOK_BASE_URL`, `WEBHOOK_SECRET`, `DATABASE_URL`,
`NVIDIA_API_KEY`, `NVIDIA_MODEL` (optional), `OPENROUTER_API_KEY` /
`OPENROUTER_MODEL` (optional), `GROQ_API_KEY` / `GROQ_MODEL` (optional),
`LLM_PROVIDER_ORDER` (optional), `LLM_COOLDOWN_MINUTES` (optional),
`PAIRS_FUZZY_THRESHOLD` (optional), `ADMIN_ID`,
`LLM_MAX_PARALLEL` (optional), `VOICE_REPLIES` (optional), `POLLING` (optional),
`PORT` (optional, default 7860).

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill values, set POLLING=1
python -m app.main     # or: uvicorn app.main:app --port 7860
```

With `POLLING=1` no public URL is needed; the schema is created automatically
on first start.

## Deploy on Hugging Face Spaces

1. Create a new Space → SDK: **Docker**.
2. Push this folder's contents to the Space repo.
3. Settings → **Variables and secrets**: add every env var above
   (`WEBHOOK_BASE_URL` = the Space's public URL, no trailing slash).
4. The Dockerfile already serves on port 7860; the bot sets its own webhook
   on startup.
5. Open the Space logs once — look for "webhook set" and no DB errors.

## Teaching her (owner tools)

- **/teach** (ADMIN_ID only): `/teach <sawal> => <jawab>` saves a *global*
  pair. Learned-from-chat pairs stay scoped to their own chat; seeds
  (~22 small-talk pairs) load automatically on first startup.
- **Sticker pool**: in DM, send the bot a sticker with a mood word as the
  caption (`happy`, `laugh`, `shy`, `sad`, `angry`, `love`, `bored`) — or
  reply to a sticker with just the mood word. She'll use pool stickers as
  replies by mood; empty mood = text only.
- **/own**: users reply to any sticker with `/own` (or `/own new`, `/own 2`,
  `/own list`) to build their own sticker packs.

## Media

Stickers are stored **only as Telegram `file_id` + mood** in the `stickers`
table — never as image binaries in the database. Note that file_ids are
per-bot: if the bot (or its token) ever changes, the sticker pool stops
working and must be re-seeded — the owner simply re-sends the stickers to
the new bot.

## Memory & retention

Short-term chat history (the raw messages fed to the LLM as context)
expires after **24 hours**: context building only uses messages newer
than 24h (anything older reaches the model only through the rolling
summary), and stored raw messages older than 24h are pruned at startup
and hourly. Learned pairs and per-user summaries are **permanent** and
are never cleaned.
