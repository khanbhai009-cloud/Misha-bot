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
  match), then the older pg_trgm similarity as a last resort. Both
  fuzzy layers pass a content-word guard before a candidate is
  accepted (see guard_accepts): fuzzy scores light up on shared
  filler — "...ho raha hai" — so a candidate only counts when the
  stimulus's distinctive words actually appear in the input;
  a rejected candidate is just a miss and the next layer / the LLM
  takes over.
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


# --------------------------------------------------------- fuzzy guard ----
# token_set_ratio (and pg_trgm) happily match on shared filler: the
# brand-new sentence "mera phone kho gaya, bohot tension ho raha hai"
# scored 81.5 against the seed stimulus "bore ho raha hai" purely via
# the shared tail "ho raha hai", and the bot answered a lost-phone
# message with the boredom pair. The guard below is the acceptance
# test BOTH fuzzy layers must pass: the stimulus's DISTINCTIVE tokens
# — its content words, i.e. everything that is not an ultra-common
# Hinglish function/aux word — must actually show up in the input.

_GUARD_STOPWORDS = frozenset({
    "ho", "raha", "rahi", "rahe", "hai", "hain", "h", "hu", "hun",
    "kya", "ka", "ki", "ke", "me", "mein", "tum", "mai", "main",
    "aaj", "kal", "bohot", "bahut", "bhi", "toh", "aur", "ek",
    "ye", "yeh", "wo", "woh", "na", "ne", "se", "par", "ko", "yaar",
    "abhi", "kuch", "mera", "meri", "mere", "tera", "teri",
    "uska", "uski", "kar",
})


def distinctive_tokens(norm_text: str) -> set:
    """The content words of a normalised phrase: every token that is
    not an ultra-common Hinglish function/aux word."""
    return {tok for tok in norm_text.split() if tok not in _GUARD_STOPWORDS}


def guard_accepts(input_norm: str, stimulus_norm: str) -> bool:
    """Acceptance guard for one fuzzy candidate (pure, no DB).

    Both sides are normalised here (normalize() is idempotent, and
    stored stimuli written by older normaliser versions get judged by
    their modern form). Rules:
    - The stimulus HAS distinctive tokens: with 1-2 of them, ALL must
      appear among the input's tokens; with 3 or more, at least 60%
      must appear.
    - The stimulus has NO distinctive tokens (a pure function-word
      phrase like "kya kar rahi ho", where a fuzzy score is nothing
      but filler overlap): the input must be that same phrase modulo
      extra function words — every stimulus token present in the
      input, and the input carrying no content words of its own.
      Anything else is rejected; only an exact-layer hit (or the LLM)
      may answer such phrases in their longer forms.
    """
    inp = normalize(input_norm)
    stim = normalize(stimulus_norm)
    if not inp or not stim:
        return False
    distinctive = distinctive_tokens(stim)
    input_tokens = set(inp.split())
    if not distinctive:
        return set(stim.split()) <= input_tokens and not distinctive_tokens(inp)
    present = distinctive & input_tokens
    if len(distinctive) <= 2:
        return len(present) == len(distinctive)
    return len(present) / len(distinctive) >= 0.6


def fuzzy_best_guarded(norm: str, choices, threshold: float = RF_THRESHOLD) -> tuple | None:
    """Best RapidFuzz match that also passes the content-word guard.

    Candidates are scored exactly like fuzzy_best(); walking them in
    score order, the first one at or above `threshold` that passes
    guard_accepts() wins. A candidate at/above threshold that the
    guard rejects is logged and skipped — it never produces a reply;
    if nothing acceptable remains, the layer is simply a miss and the
    next layer (or the LLM) takes over. Returns (choice, score) or
    None, like fuzzy_best().
    """
    if not _RF_AVAILABLE or not norm:
        return None
    scored = []
    for choice in choices:
        if not choice:
            continue
        score = max(
            _rf_fuzz.ratio(norm, choice),
            _rf_fuzz.token_set_ratio(norm, choice),
        )
        scored.append((choice, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    for choice, score in scored:
        if score < threshold:
            break
        if guard_accepts(norm, choice):
            return (choice, score)
        log.info("pair fuzzy rejected (guard): '%s' vs '%s'", norm, choice)
    return None


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
    enough to the input (similarity above FUZZY_SIMILARITY_THRESHOLD).
    stimulus_norm rides along so lookup() can run the guard on it."""
    return (
        "SELECT id, stimulus_norm, response, mood, hits, similarity(stimulus_norm, $2) AS sim "
        "FROM learned_pairs "
        "WHERE scope = $1 AND similarity(stimulus_norm, $2) > $3 "
        "ORDER BY sim DESC, hits DESC LIMIT 1"
    )


async def _fetch_fuzzy(scope: str, norm: str) -> dict | None:
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(_fuzzy_query(), scope, norm, FUZZY_SIMILARITY_THRESHOLD)
    return dict(row) if row else None


async def _fetch_fuzzy_guarded(scope: str, norm: str) -> dict | None:
    """The pg_trgm last-resort match for one scope, through the same
    content-word guard as the RapidFuzz layer: a similar-enough row
    whose distinctive words do not actually appear in the input is a
    miss, not a reply."""
    row = await _fetch_fuzzy(scope, norm)
    if row is None:
        return None
    if not guard_accepts(norm, row.get("stimulus_norm") or ""):
        log.info(
            "pair fuzzy rejected (guard): '%s' vs '%s'",
            norm, row.get("stimulus_norm"),
        )
        return None
    return row


async def _rf_best_stimulus(scope: str, norm: str) -> str | None:
    """RapidFuzz scan of one scope's stored stimuli: fetch the distinct
    stimulus_norm values (most-hit first, capped) and return the best
    one that scores at or above RF_THRESHOLD *and* passes the
    content-word guard, else None."""
    if not _RF_AVAILABLE:
        return None
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT stimulus_norm FROM learned_pairs WHERE scope = $1 "
            "GROUP BY stimulus_norm ORDER BY SUM(hits) DESC LIMIT $2",
            scope, RF_CANDIDATE_CAP,
        )
    best = fuzzy_best_guarded(norm, [r["stimulus_norm"] for r in rows])
    if best:
        return best[0]
    return None


async def lookup(chat_id: int, text: str) -> dict | None:
    """Find a learned response for this message, or None.

    Order: exact match (chat scope, then global) -> RapidFuzz fuzzy
    over the scope's stimuli (same scope order) -> pg_trgm fuzzy as a
    last resort. Both fuzzy layers are gated by the content-word
    guard (guard_accepts): a candidate whose distinctive words do not
    appear in the input is rejected and lookup simply falls through,
    exactly like a miss. Among up to 3 stored responses for the
    matched stimulus, pick weighted by hits (popular answers surface
    more), and count the hit. The returned dict also carries a "via"
    tag ("exact" | "rapidfuzz" | "pg_trgm") naming which layer
    matched — for logging only; reply behaviour is unchanged.
    """
    norm = normalize(text)
    if not norm:
        return None
    via = "exact"
    candidates = await _fetch_candidates(_chat_scope(chat_id), norm)
    if not candidates:
        candidates = await _fetch_candidates(GLOBAL_SCOPE, norm)
    if not candidates:
        via = "rapidfuzz"
        for scope in (_chat_scope(chat_id), GLOBAL_SCOPE):
            stimulus = await _rf_best_stimulus(scope, norm)
            if stimulus:
                candidates = await _fetch_candidates(scope, stimulus)
                if candidates:
                    break
    if not candidates:
        fuzzy = await _fetch_fuzzy_guarded(_chat_scope(chat_id), norm)
        if fuzzy is None:
            fuzzy = await _fetch_fuzzy_guarded(GLOBAL_SCOPE, norm)
        if fuzzy is None:
            return None
        candidates = [fuzzy]
        via = "pg_trgm"
    weights = [int(c.get("hits") or 0) + 1 for c in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    async with db.pool().acquire() as conn:
        await conn.execute(
            "UPDATE learned_pairs SET hits = hits + 1 WHERE id = $1", chosen["id"]
        )
    return {"reply": chosen["response"], "mood": chosen.get("mood") or "happy", "via": via}


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
