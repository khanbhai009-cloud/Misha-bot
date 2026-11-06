"""DB-trained stimulus -> response pairs — the "seekhi hui baatein" layer.

How it works (modelled on how the old retrieval bots "learned"):
- Every chat message is normalised (lowercase, no punctuation, no bot
  mention, single spaces) and looked up in `learned_pairs`.
- HIT  -> the stored response is sent directly. No LLM call, no cost,
          no waiting on anybody's daily request quota.
- MISS -> the LLM answers (handlers/chat.py), and the exchange is then
          learned back with source='auto', so the same sawal next time
          is free. This is how the layer trains itself from live chat.
- The owner can hand-teach exact pairs with /teach (source='teach').
- Lookup falls back to fuzzy matching when no exact pair hits: first a
  RapidFuzz pass over the scope's stored stimuli (max of ratio and
  token_set_ratio, so spelling slips, short forms and extra words still
  match), then the older pg_trgm similarity as a last resort.
- Auto-learned pairs pass a safety filter first (links, phone numbers
  and gaaliyan are never learned from live chat); /teach pairs and the
  seeds bypass it — the owner is trusted.
- On first startup ~22 common Hinglish small-talk pairs are inserted as
  source='seed' so a brand-new bot already handles hii/kaise ho/etc.

Scopes: pairs learned inside a chat stay in that chat's scope first
(group apni language/style seekhta hai), global pairs are the fallback.
"""
from __future__ import annotations

import logging
import os
import random
import re

from . import db
from .persona import BOT_NAME

log = logging.getLogger("bot.pairs")

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _RF_AVAILABLE = True
except Exception:  # pragma: no cover - requirements pin rapidfuzz; safety net
    _rf_fuzz = None
    _RF_AVAILABLE = False
    log.warning("rapidfuzz not importable — pairs lookup uses pg_trgm only")

GLOBAL_SCOPE = "global"

_MENTION_RE = re.compile(r"@\w+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")

# Fuzzy fallback threshold (pg_trgm similarity, 0..1). Env-tunable.
try:
    FUZZY_SIMILARITY_THRESHOLD = float(os.getenv("PAIRS_FUZZY_THRESHOLD", "0.45") or 0.45)
except ValueError:
    FUZZY_SIMILARITY_THRESHOLD = 0.45

# RapidFuzz hit threshold (score 0..100) and the per-scope candidate cap
# for the in-process scan. Env-tunable.
try:
    RF_THRESHOLD = float(os.getenv("PAIRS_RF_THRESHOLD", "78") or 78)
except ValueError:
    RF_THRESHOLD = 78.0
RF_CANDIDATE_CAP = 5000

# ---------------------------------------------------------- safety filter ----
# Auto-learning drinks from live group chat, which is full of promo spam,
# phone-number drops and gaaliyan. Anything smelling like that is refused
# at learn time (source='auto' only — the owner's /teach pairs and the
# seeds bypass this filter). The URL/phone checks run on the RAW text:
# normalisation would destroy the dots and plus signs they are recognised
# by. The gaali check runs on normalised tokens.

_URL_RE = re.compile(r"https?://|www\.|t\.me/|[a-z0-9-]+\.[a-z]{2,}(/\S*)?", re.IGNORECASE)
_PHONE_RE = re.compile(r"(\+?91[\s-]?|0)?(\d[\s-]?){7,}\d")

_PROFANITY = {
    # English
    "fuck", "fucking", "fucked", "shit", "bitch", "bastard", "asshole",
    "dick", "pussy", "whore", "slut", "porn", "xxx", "nigger", "nigga",
    # Hinglish gaaliyan (roman script, as they show up in group chat)
    "madarchod", "behenchod", "bhenchod", "bhosdike", "bhosdi",
    "chutiya", "chutiye", "chut", "lund", "gaand", "randi", "harami",
    "chod", "chodu", "jhaat",
}

_rejected_auto = 0  # running count of auto-learn pairs the filter refused


def _pair_is_unsafe(stimulus_text: str, response_text: str) -> bool:
    """True when an auto-learn candidate carries a link, a phone number
    or a gaali — in the stimulus or in the response."""
    for raw in (stimulus_text or "", response_text or ""):
        lowered = raw.lower()
        if _URL_RE.search(lowered) or _PHONE_RE.search(lowered):
            return True
        if set(normalize(raw).split()) & _PROFANITY:
            return True
    return False


_REPEAT_RE = re.compile(r"(.)\1{2,}")

# Conservative Hinglish short-form map, applied to standalone tokens
# only. Keep entries unambiguous: a token here must (almost) always mean
# the long form in group-chat Hinglish.
_SHORT_FORMS = {
    "tm": "tum",
    "kyu": "kyun", "kyo": "kyun",
    "nhi": "nahi",
    "bht": "bahut",
    "acha": "accha", "achha": "accha",
    "thik": "theek",
    "kr": "kar",
    "rhi": "rahi", "rha": "raha", "rhe": "rahe",
    "mjhe": "mujhe", "tjhe": "tujhe",
    "yr": "yaar",
    "plz": "please", "pls": "please",
    "kb": "kab", "jb": "jab", "tb": "tab",
    "krna": "karna",
    "kse": "kaise", "kesa": "kaisa", "kesi": "kaisi",
    "smjh": "samajh",
    "mt": "mat",
}


def normalize(text: str) -> str:
    """Forgiving normalisation for pair matching.

    'Hii!! @Misha kya haal hai??' -> 'hii kya haal hai'
    'tm kya kr rhi ho'            -> 'tum kya kar rahi ho'

    Steps: lowercase, drop mentions and punctuation, collapse 3+
    repeated letters to 2 ('hiiii' -> 'hii'), expand common Hinglish
    short forms token by token. NOTE: rows already stored in
    learned_pairs were written with the older, simpler normaliser, so
    their stimulus_norm can differ slightly from what this function
    produces today — the RapidFuzz fuzzy layer in lookup() absorbs
    that drift, which is why exact misses still resolve there.
    """
    t = (text or "").lower()
    t = _MENTION_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    t = _SPACES_RE.sub(" ", t).strip()
    t = _REPEAT_RE.sub(r"\1\1", t)
    if not t:
        return ""
    return " ".join(_SHORT_FORMS.get(tok, tok) for tok in t.split(" "))


def fuzzy_best(norm: str, choices) -> tuple | None:
    """Best RapidFuzz match for `norm` among `choices` (pure, no DB).

    Score per candidate is max(ratio, token_set_ratio): ratio catches
    spelling slips ('kse ho' ~ 'kaise ho'), token_set_ratio ignores
    extra/missing words and word order ('kya haal hai yaar' ~
    'kya haal hai'). Returns (choice, score 0..100) or None.
    """
    if not _RF_AVAILABLE or not norm:
        return None
    best_choice = None
    best_score = -1.0
    for choice in choices:
        if not choice:
            continue
        score = max(
            _rf_fuzz.ratio(norm, choice),
            _rf_fuzz.token_set_ratio(norm, choice),
        )
        if score > best_score:
            best_choice, best_score = choice, score
    if best_choice is None:
        return None
    return (best_choice, best_score)


def is_unsafe_text(text: str) -> bool:
    """Public wrapper over the auto-learn safety filter (stimulus only).
    Used by the group harvester for sticker associations."""
    return _pair_is_unsafe(text or "", "")


def _chat_scope(chat_id: int) -> str:
    return str(chat_id)


async def _fetch_candidates(scope: str, norm: str) -> list:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, response, mood, hits FROM learned_pairs "
            "WHERE scope = $1 AND stimulus_norm = $2 "
            "ORDER BY hits DESC LIMIT 3",
            scope, norm,
        )
    return [dict(r) for r in rows]


def _fuzzy_query() -> str:
    """pg_trgm fuzzy match: the single best stored stimulus similar
    enough to the input (similarity above FUZZY_SIMILARITY_THRESHOLD)."""
    return (
        "SELECT id, response, mood, hits, similarity(stimulus_norm, $2) AS sim "
        "FROM learned_pairs "
        "WHERE scope = $1 AND similarity(stimulus_norm, $2) > $3 "
        "ORDER BY sim DESC, hits DESC LIMIT 1"
    )


async def _fetch_fuzzy(scope: str, norm: str) -> dict | None:
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(_fuzzy_query(), scope, norm, FUZZY_SIMILARITY_THRESHOLD)
    return dict(row) if row else None


async def _rf_best_stimulus(scope: str, norm: str) -> str | None:
    """RapidFuzz scan of one scope's stored stimuli: fetch the distinct
    stimulus_norm values (most-hit first, capped) and return the best
    one scoring at or above RF_THRESHOLD, else None."""
    if not _RF_AVAILABLE:
        return None
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT stimulus_norm FROM learned_pairs WHERE scope = $1 "
            "GROUP BY stimulus_norm ORDER BY SUM(hits) DESC LIMIT $2",
            scope, RF_CANDIDATE_CAP,
        )
    best = fuzzy_best(norm, [r["stimulus_norm"] for r in rows])
    if best and best[1] >= RF_THRESHOLD:
        return best[0]
    return None


async def lookup(chat_id: int, text: str) -> dict | None:
    """Find a learned response for this message, or None.

    Order: exact match (chat scope, then global) -> RapidFuzz fuzzy
    over the scope's stimuli (same scope order) -> pg_trgm fuzzy as a
    last resort. Among up to 3 stored responses for the matched
    stimulus, pick weighted by hits (popular answers surface more),
    and count the hit.
    """
    norm = normalize(text)
    if not norm:
        return None
    candidates = await _fetch_candidates(_chat_scope(chat_id), norm)
    if not candidates:
        candidates = await _fetch_candidates(GLOBAL_SCOPE, norm)
    if not candidates:
        for scope in (_chat_scope(chat_id), GLOBAL_SCOPE):
            stimulus = await _rf_best_stimulus(scope, norm)
            if stimulus:
                candidates = await _fetch_candidates(scope, stimulus)
                if candidates:
                    break
    if not candidates:
        fuzzy = await _fetch_fuzzy(_chat_scope(chat_id), norm)
        if fuzzy is None:
            fuzzy = await _fetch_fuzzy(GLOBAL_SCOPE, norm)
        if fuzzy is None:
            return None
        candidates = [fuzzy]
    weights = [int(c.get("hits") or 0) + 1 for c in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    async with db.pool().acquire() as conn:
        await conn.execute(
            "UPDATE learned_pairs SET hits = hits + 1 WHERE id = $1", chosen["id"]
        )
    return {"reply": chosen["response"], "mood": chosen.get("mood") or "happy"}


async def learn(chat_id, stimulus_text: str, response_text: str, source: str = "auto",
                created_by=None, mood: str | None = None) -> bool:
    """Store one stimulus -> response pair. Scope is the chat for 'auto'
    learning, global for /teach. Exact duplicates just bump hits.
    Auto-learned pairs must pass the safety filter (no links, phone
    numbers or gaaliyan); /teach and seeds are trusted and skip it."""
    global _rejected_auto
    norm = normalize(stimulus_text)
    response = (response_text or "").strip()
    if not norm or not response or len(norm) > 300:
        return False
    if source == "auto" and _pair_is_unsafe(stimulus_text, response_text):
        _rejected_auto += 1
        log.debug(
            "auto-learn rejected by safety filter (total refused: %d): %r",
            _rejected_auto, (stimulus_text or "")[:80],
        )
        return False
    scope = GLOBAL_SCOPE if source == "teach" else _chat_scope(chat_id)
    async with db.pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO learned_pairs (scope, stimulus_norm, stimulus_raw, response, mood, source, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (scope, stimulus_norm, response) "
            "DO UPDATE SET hits = learned_pairs.hits + 1",
            scope, norm, (stimulus_text or "")[:300], response[:1000], mood, source, created_by,
        )
    return True


# ------------------------------------------------------------------ seeds ----

# Common Hinglish small talk, persona-consistent (see persona.py). These are
# inserted only when learned_pairs is completely empty (first startup).
SEED_PAIRS = [
    ("hii", "hieee 😊 kya chal raha hai?", "happy"),
    ("hi", "hello hello! kya haal hai?", "happy"),
    ("hello", "hellooo 👋 batao batao, kya scene hai?", "happy"),
    ("hey", "heyy 😌 kya kar rahe ho?", "happy"),
    ("kaise ho", "main badhiya hu 😊 tum batao, kya chal raha hai?", "happy"),
    ("kaisi ho", "ekdum mast hu 😌 tum sunao?", "happy"),
    ("kya haal hai", "sab badhiya ✨ tum sunao kya chal raha hai", "happy"),
    ("good morning", "good morninggg ☀️ uth gaye tum bhi meri tarah?", "happy"),
    ("good night", "good night 💤 acche sapne dekhna, kal milte hain", "bored"),
    ("good evening", "good evening 😊 chai pi ya bas phone chala rahe ho?", "happy"),
    ("kya kar rahi ho", "bas yahi hu, tumse baat kar rahi hu 😌 aur tum?", "shy"),
    ("kya kar rhi ho", "kuch khaas nahi, tum batao kya chal raha hai 😊", "happy"),
    ("naam kya hai", "main " + BOT_NAME + " hu 😊 tum apna naam batao na", "shy"),
    ("tumhara naam kya hai", BOT_NAME + "! 💕 tum apna naam batao", "happy"),
    ("tum kaun ho", "main " + BOT_NAME + " hu, tumhari chat wali dost 😊", "happy"),
    ("bot ho kya", "bot?? main toh " + BOT_NAME + " hu 😏 kya bol rahe ho", "laugh"),
    ("bye", "byeee 👋 phir milenge, dhyaan rakho", "happy"),
    ("thank you", "koi baat nahi 😊 anytime!", "happy"),
    ("thanks", "welcome welcome 😌", "happy"),
    ("aur batao", "bas kuch khaas nahi 😌 tum sunaao kuch interesting", "happy"),
    ("kahan se ho", "main toh yahi ki hu, is chat ki hi 😂 tum batao", "laugh"),
    ("khana khaya", "abhi kahan, tumne khaya? 😊", "happy"),
    ("bore ho raha hai", "toh /bomb khele? 💣 group me bada maza aata hai", "bored"),
    ("i love you", "uff, itni jaldi? 😳💕 pehle dost ban jao", "shy"),
    ("pagal ho", "tumse toh kam hi hu 😏", "laugh"),
]


async def ensure_seeds() -> None:
    """Insert seed pairs on first startup only (when the table is empty)."""
    async with db.pool().acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM learned_pairs")
        if count:
            return
        for stimulus, response, mood in SEED_PAIRS:
            await conn.execute(
                "INSERT INTO learned_pairs (scope, stimulus_norm, stimulus_raw, response, mood, source) "
                "VALUES ($1, $2, $3, $4, $5, 'seed') "
                "ON CONFLICT (scope, stimulus_norm, response) DO NOTHING",
                GLOBAL_SCOPE, normalize(stimulus), stimulus, response, mood,
            )
    log.info("learned_pairs seeded with %d pairs", len(SEED_PAIRS))
