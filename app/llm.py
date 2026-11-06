"""LLM layer — multi-provider rotation over the user's own free tiers.

Pattern copied from the user's other systems: never depend on a single
key. Providers are tried in LLM_PROVIDER_ORDER (default
"nvidia,openrouter,groq") — all three are OpenAI-compatible chat
completions endpoints running on the user's own keys:

- nvidia — NVIDIA_API_KEY / NVIDIA_MODEL (integrate.api.nvidia.com).
- openrouter — OPENROUTER_API_KEY / OPENROUTER_MODEL.
- groq — GROQ_API_KEY / GROQ_MODEL, sent with a browser-like User-Agent
  (his Groq access 403s without one from some networks).

A provider that answers HTTP 429 or a quota/RESOURCE_EXHAUSTED error is
parked in an in-memory cooldown (LLM_COOLDOWN_MINUTES, default 60) and
the chain moves to the next provider. If every provider fails or is
cooling down, _generate() returns None and callers fall back to canned
Hinglish lines — chat never crashes. The DB pairs layer sits in front of
this module and never touches a provider at all.

Output contract for chat, identical across providers: the model answers
with JSON {"reply": "...", "mood": "...", "sticker": true/false}.
Parsing is defensive — code fences are stripped and any failure falls
back to a canned line.

A global semaphore caps parallel LLM calls (LLM_MAX_PARALLEL, default
10): 100 groups tagging at once queue up here instead of melting the
API quota or the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import httpx

from .config import get_settings
from .gender import gender_fact_line
from .persona import FALLBACK_REPLIES, MOODS, PERSONA_PROMPT

log = logging.getLogger("bot.llm")

_NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_KNOWN_PROVIDERS = ("nvidia", "openrouter", "groq")

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
_cooldown_until: dict = {}


class _QuotaHit(Exception):
    """Provider said 429 / quota exhausted — park it and move on."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(25.0))
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().llm_max_parallel)
    return _semaphore


# -------------------------------------------------------------- rotation ----

def _provider_order() -> list:
    raw = get_settings().llm_provider_order or ""
    order = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name in _KNOWN_PROVIDERS and name not in order:
            order.append(name)
    return order or ["nvidia"]


def _in_cooldown(name: str) -> bool:
    return _cooldown_until.get(name, 0.0) > time.monotonic()


def provider_status() -> list:
    """Rotation state for the /admin overview: one dict per provider
    in configured order (name, model, whether a key is set, cooldown)."""
    settings = get_settings()
    models = {
        "nvidia": settings.nvidia_model,
        "openrouter": settings.openrouter_model,
        "groq": settings.groq_model,
    }
    keys = {
        "nvidia": settings.nvidia_api_key,
        "openrouter": settings.openrouter_api_key,
        "groq": settings.groq_api_key,
    }
    return [
        {
            "name": name,
            "model": models.get(name, ""),
            "has_key": bool(keys.get(name)),
            "cooldown": _in_cooldown(name),
        }
        for name in _provider_order()
    ]


def _mark_cooldown(name: str) -> None:
    minutes = max(1, get_settings().llm_cooldown_minutes)
    _cooldown_until[name] = time.monotonic() + minutes * 60
    log.warning("provider %s parked in cooldown for %d min", name, minutes)


def _is_quota(status_code: int, body: str) -> bool:
    if status_code == 429:
        return True
    return "quota" in (body or "").lower()


async def _call_openai_compatible(url: str, api_key: str, model: str,
                                  prompt_text: str, system_text: str,
                                  max_tokens: int, extra_headers: dict | None = None):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.9,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
    if extra_headers:
        headers.update(extra_headers)
    # Per-attempt timeout (LLM_PROVIDER_TIMEOUT_SECONDS, default 10s):
    # one hanging provider must never stall a reply for the shared
    # client's full 25s — the chain rotates when this fires.
    resp = await _get_client().post(
        url, json=payload, headers=headers,
        timeout=get_settings().llm_provider_timeout_seconds,
    )
    if resp.status_code != 200:
        if _is_quota(resp.status_code, resp.text):
            raise _QuotaHit("http " + str(resp.status_code) + " from " + url)
        raise RuntimeError("http " + str(resp.status_code) + " from " + url + ": " + resp.text[:200])
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content")
    return content if content else None


async def _call_provider(name: str, prompt_text: str, system_text: str, max_tokens: int):
    """One provider attempt. Returns text, None (skip/empty), or raises
    _QuotaHit / RuntimeError — the chain in _generate decides what's next."""
    settings = get_settings()
    if name == "nvidia":
        if not settings.nvidia_api_key:
            return None
        return await _call_openai_compatible(
            _NVIDIA_URL, settings.nvidia_api_key, settings.nvidia_model,
            prompt_text, system_text, max_tokens,
        )
    if name == "openrouter":
        if not settings.openrouter_api_key:
            return None
        return await _call_openai_compatible(
            _OPENROUTER_URL, settings.openrouter_api_key, settings.openrouter_model,
            prompt_text, system_text, max_tokens,
            extra_headers={"X-Title": "Misha Telegram Bot"},
        )
    if name == "groq":
        if not settings.groq_api_key:
            return None
        return await _call_openai_compatible(
            _GROQ_URL, settings.groq_api_key, settings.groq_model,
            prompt_text, system_text, max_tokens,
            extra_headers={"User-Agent": _BROWSER_UA},
        )
    return None


async def _generate(prompt_text: str, system_text: str, max_tokens: int) -> str | None:
    """Try each provider in order; None when the whole chain is down."""
    async with _get_semaphore():
        for name in _provider_order():
            if _in_cooldown(name):
                continue
            started = time.monotonic()
            try:
                text = await _call_provider(name, prompt_text, system_text, max_tokens)
            except _QuotaHit:
                log.warning("provider %s hit its quota — rotating", name)
                _mark_cooldown(name)
                continue
            except Exception:
                log.exception("provider %s failed — trying next", name)
                continue
            if text:
                log.info(
                    "provider %s answered in %.1fs",
                    name, time.monotonic() - started,
                )
                return text
            log.info("provider %s returned empty — rotating", name)
    return None


# -------------------------------------------------------------- chat API ----

def _fallback() -> dict:
    return {"reply": random.choice(FALLBACK_REPLIES), "mood": "happy", "sticker": False}


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    # Strip ```json fences if the model wrapped its answer.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Last resort: first {...} block in the text.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return _fallback()
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return _fallback()
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return _fallback()
    mood = str(data.get("mood") or "happy").strip().lower()
    if mood not in MOODS:
        mood = "happy"
    return {"reply": reply[:500], "mood": mood, "sticker": bool(data.get("sticker"))}


async def chat_reply(summary: str, history: list, user_text: str, user_name: str = "",
                     gender: str | None = None) -> dict:
    """One persona reply for one user message. Never raises.

    `gender` is the code-detected user gender ('M'/'F'/None, see
    app/gender.py). When known it goes in as a FACT line at the very
    top of the context — the prompt-level rule alone kept slipping,
    so the model now gets the user's gender as a stated fact instead
    of something it has to infer.
    """
    lines = []
    fact = gender_fact_line(gender)
    if fact:
        lines.append(fact)
    if summary:
        lines.append("Purani baato ka summary: " + summary)
    for item in history or []:
        who = user_name or "User" if item.get("role") == "user" else "Tum"
        lines.append(who + ": " + str(item.get("text", "")))
    lines.append((user_name or "User") + ": " + user_text)
    lines.append("Ab tumhara jawab (sirf JSON):")
    try:
        raw = await _generate("\n".join(lines), PERSONA_PROMPT, max_tokens=1000)
    except Exception:
        log.exception("LLM chain failed")
        return _fallback()
    if not raw:
        return _fallback()
    return _parse(raw)


async def summarize(old_summary: str, dropped: list) -> str:
    """Fold trimmed messages into the rolling summary (best effort)."""
    if not dropped:
        return old_summary
    convo = "\n".join(
        ("User: " if m.get("role") == "user" else "Bot: ") + str(m.get("text", ""))
        for m in dropped
    )
    prompt = (
        "Purana summary: " + (old_summary or "(kuch nahi)") + "\n\n"
        "Nayi baate jo summary me jodni hai:\n" + convo + "\n\n"
        "Ek chhota sa updated summary banao (max 40 words, Hinglish chalega) — "
        "user ka naam, pasand/napasand, chal rahi important baate. "
        'Sirf JSON do: {"summary": "..."}'
    )
    try:
        raw = await _generate(prompt, "You compress chat memory into tiny summaries.", max_tokens=1000)
        if raw:
            data = json.loads(re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()))
            summary = str(data.get("summary") or "").strip()
            if summary:
                return summary[:600]
    except Exception:
        log.exception("Summarise failed — keeping old summary")
    return old_summary
