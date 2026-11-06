-- Friendly chat bot schema (Supabase Postgres)
-- Applied automatically on startup by app/db.py (init_schema).

-- pg_trgm powers the fuzzy fallback in the learned-pairs lookup
-- (similarity() over stimulus_norm, see app/pairs.py).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS users (
    user_id         BIGINT PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    coins           BIGINT NOT NULL DEFAULT 0,
    gems            NUMERIC NOT NULL DEFAULT 0,
    xp              INT NOT NULL DEFAULT 0,
    kills           INT NOT NULL DEFAULT 0,
    last_daily      TIMESTAMPTZ,
    protected_until TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- House account: escrow for game stakes and house cut. Never a real user.
INSERT INTO users (user_id, username, first_name)
VALUES (0, 'house', 'House')
ON CONFLICT (user_id) DO NOTHING;

-- Per (chat, user) conversation memory for the LLM chat layer.
CREATE TABLE IF NOT EXISTS chats (
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    messages   JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);

-- Sticker pool for the chat persona, grouped by mood.
CREATE TABLE IF NOT EXISTS stickers (
    id         SERIAL PRIMARY KEY,
    file_id    TEXT NOT NULL UNIQUE,
    mood       TEXT NOT NULL,
    added_by   BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stickers_mood ON stickers (mood);

-- Sticker packs created for users via /own.
CREATE TABLE IF NOT EXISTS user_packs (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    pack_index INT NOT NULL,
    pack_name  TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    format     TEXT NOT NULL DEFAULT 'static',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, pack_index)
);

-- Group games (bomb). State lives here so a restart never corrupts balances;
-- stakes are escrowed to the house row (user_id = 0) while a game runs.
CREATE TABLE IF NOT EXISTS games (
    id               SERIAL PRIMARY KEY,
    chat_id          BIGINT NOT NULL,
    creator_id       BIGINT NOT NULL,
    amount           BIGINT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'lobby',  -- lobby | running | finished | cancelled
    current_holder   BIGINT,
    winner_id        BIGINT,
    lobby_message_id BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_games_chat_status ON games (chat_id, status);

CREATE TABLE IF NOT EXISTS game_players (
    game_id  BIGINT NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    user_id  BIGINT NOT NULL,
    alive    BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, user_id)
);

-- Learned stimulus -> response pairs (the "DB-trained" chat layer).
-- scope is 'global' or a chat_id as text. A hit here answers a message
-- with ZERO LLM calls; misses fall through to the LLM and are then
-- learned back as source='auto'.
CREATE TABLE IF NOT EXISTS learned_pairs (
    id            BIGSERIAL PRIMARY KEY,
    scope         TEXT NOT NULL,
    stimulus_norm TEXT NOT NULL,
    stimulus_raw  TEXT,
    response      TEXT NOT NULL,
    mood          TEXT,
    source        TEXT NOT NULL DEFAULT 'auto',  -- seed | teach | auto
    hits          INT NOT NULL DEFAULT 0,
    created_by    BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, stimulus_norm, response)
);
CREATE INDEX IF NOT EXISTS idx_pairs_scope_stimulus ON learned_pairs (scope, stimulus_norm);
CREATE INDEX IF NOT EXISTS idx_pairs_stimulus_trgm ON learned_pairs USING GIN (stimulus_norm gin_trgm_ops);
