"""Admin dashboard — server-rendered pages under /admin.

Full read access to the bot's data (overview stats, learned pairs,
users, sticker pool + harvested sticker associations) plus pair
add/delete, matching what /teach does from Telegram.

Security model:
- The panel exists ONLY when the ADMIN_PANEL_KEY env var is set. While
  it is empty, every /admin route answers 404 — the panel is not
  discoverable and the bot behaves exactly as before.
- Login is a single form posting the key. On success the browser gets
  an HttpOnly cookie holding an HMAC of a fixed label under that key
  (stateless — nothing is stored server-side, and the cookie value
  reveals nothing about the key). Key comparison uses
  hmac.compare_digest.
- No JavaScript, no external assets, no new dependencies: plain HTML
  strings, form posts parsed by hand so python-multipart is not
  needed.

All list queries live in db.py (admin_* helpers); pair writes go
through pairs.learn / db.delete_pair so panel and bot never disagree.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, llm, pairs
from .config import get_settings

log = logging.getLogger("bot.admin")

router = APIRouter(prefix="/admin")

_COOKIE = "misha_admin"
_LABEL = b"misha-admin-panel-v1"
_PAGE_SIZE = 25

_CSS = (
    "body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:62rem;"
    "padding:0 1rem;color:#1c2733;background:#f7f9fb}"
    "h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}"
    "a{color:#0b62b3}nav a{margin-right:1rem}"
    "table{border-collapse:collapse;width:100%;background:#fff}"
    "th,td{border:1px solid #d5dde5;padding:.35rem .5rem;text-align:left;"
    "font-size:.85rem;vertical-align:top}"
    "th{background:#eaf0f6}.card{background:#fff;border:1px solid #d5dde5;"
    "border-radius:.5rem;padding:1rem;margin:.5rem 0}"
    ".stat{display:inline-block;min-width:9rem;margin:.25rem .5rem .25rem 0}"
    ".stat b{display:block;font-size:1.3rem}"
    "input,select,button{padding:.4rem;font-size:.9rem}"
    "input[type=text]{min-width:16rem}.mut{color:#667}.err{color:#b00020}"
    "td.txt{max-width:22rem;overflow-wrap:anywhere}code{font-size:.8rem}"
)


def _enabled() -> bool:
    return bool(get_settings().admin_panel_key)


def _token() -> str:
    key = get_settings().admin_panel_key
    return hmac.new(key.encode(), _LABEL, hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    if not _enabled():
        return False
    return hmac.compare_digest(request.cookies.get(_COOKIE, ""), _token())


def _require(request: Request) -> None:
    """404 while the panel is disabled (key unset) — panel invisible."""
    if not _enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _page(title: str, body: str) -> HTMLResponse:
    nav = (
        "<nav><a href='/admin'>Overview</a><a href='/admin/pairs'>Pairs</a>"
        "<a href='/admin/users'>Users</a><a href='/admin/stickers'>Stickers</a>"
        "<a href='/admin/logout'>Log out</a></nav>"
    )
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + html.escape(title) + " — Misha admin</title>"
        "<style>" + _CSS + "</style></head><body>" + nav +
        "<h1>" + html.escape(title) + "</h1>" + body + "</body></html>"
    )
    return HTMLResponse(doc)


def _login_page(error: str = "") -> HTMLResponse:
    err = "<p class='err'>" + html.escape(error) + "</p>" if error else ""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Misha admin</title>"
        "<style>" + _CSS + "</style></head><body><h1>Misha admin</h1>" + err +
        "<form method='post' action='/admin/login'>"
        "<label>Admin key <input type='password' name='key' autofocus></label> "
        "<button type='submit'>Log in</button></form></body></html>"
    )
    return HTMLResponse(body, status_code=401 if error else 200)


def _gate(request: Request) -> HTMLResponse | None:
    """None when the request may proceed, else the login page."""
    _require(request)
    if not _authed(request):
        return _login_page()
    return None


async def _form(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(raw)
    return {k: v[0] for k, v in parsed.items()}


def _pager(base: str, page: int, total: int, extra: str = "") -> str:
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if pages <= 1:
        return ""
    links = []
    if page > 1:
        links.append("<a href='%s?page=%d%s'>&larr; Prev</a>" % (base, page - 1, extra))
    links.append("<span class='mut'>Page %d of %d (%d total)</span>" % (page, pages, total))
    if page < pages:
        links.append("<a href='%s?page=%d%s'>Next &rarr;</a>" % (base, page + 1, extra))
    return "<p>" + " &nbsp; ".join(links) + "</p>"


# ------------------------------------------------------------------ auth ----

@router.get("/login")
async def login_form(request: Request):
    _require(request)
    if _authed(request):
        return RedirectResponse("/admin", status_code=303)
    return _login_page()


@router.post("/login")
async def login_submit(request: Request):
    _require(request)
    form = await _form(request)
    if hmac.compare_digest(form.get("key", ""), get_settings().admin_panel_key):
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            _COOKIE, _token(), httponly=True, samesite="lax",
            path="/admin", max_age=30 * 24 * 3600,
        )
        return resp
    return _login_page("Wrong key.")


@router.get("/logout")
async def logout(request: Request):
    _require(request)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(_COOKIE, path="/admin")
    return resp


# -------------------------------------------------------------- overview ----

@router.get("")
async def overview(request: Request):
    gate = _gate(request)
    if gate is not None:
        return gate
    s = await db.admin_stats()
    by_source = s.get("pairs_by_source") or {}
    stats = [
        ("Users", s["users"]),
        ("Chats seen", s["chats_seen"]),
        ("Groups seen", s["groups_seen"]),
        ("Chat memory rows", s["chat_rows"]),
        ("Learned pairs", s["pairs_total"]),
        ("Pair hits", s["pairs_hits"]),
        ("Pairs — seed", by_source.get("seed", 0)),
        ("Pairs — auto", by_source.get("auto", 0)),
        ("Pairs — teach", by_source.get("teach", 0)),
        ("Sticker pool", s["sticker_pool"]),
        ("Harvested stickers", s["harvested_total"]),
        ("Harvest observations", s["harvested_hits"]),
        ("Games total", s["games_total"]),
        ("Games finished", s["games_finished"]),
    ]
    cards = "".join(
        "<span class='stat'><b>%s</b>%s</span>" % (html.escape(str(v)), html.escape(k))
        for k, v in stats
    )
    prov_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(p["name"]),
            html.escape(p["model"]),
            "yes" if p["has_key"] else "<span class='err'>no key</span>",
            "<span class='err'>cooling down</span>" if p["cooldown"] else "ready",
        )
        for p in llm.provider_status()
    )
    body = (
        "<div class='card'>" + cards + "</div>"
        "<h2>LLM providers (rotation order)</h2>"
        "<table><tr><th>Provider</th><th>Model</th><th>Key set</th>"
        "<th>State</th></tr>" + prov_rows + "</table>"
    )
    return _page("Overview", body)


# ----------------------------------------------------------------- pairs ----

@router.get("/pairs")
async def pairs_page(request: Request, q: str = "", scope: str = "",
                     source: str = "", page: int = 1):
    gate = _gate(request)
    if gate is not None:
        return gate
    page = max(1, page)
    rows, total = await db.list_pairs(
        q=q, scope=scope, source=source,
        limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE,
    )
    trs = []
    for r in rows:
        trs.append(
            "<tr><td>%d</td><td>%s</td><td>%s</td>"
            "<td class='txt'>%s</td><td class='txt'>%s</td><td>%s</td><td>%d</td>"
            "<td><form method='post' action='/admin/pairs/delete' "
            "onsubmit=\"return confirm('Delete this pair?')\">"
            "<input type='hidden' name='id' value='%d'>"
            "<button type='submit'>Delete</button></form></td></tr>" % (
                r["id"], html.escape(str(r["scope"])), html.escape(str(r["source"])),
                html.escape(str(r["stimulus_raw"] or r["stimulus_norm"])),
                html.escape(str(r["response"])),
                html.escape(str(r["mood"] or "")), int(r["hits"] or 0), r["id"],
            )
        )
    extra = ""
    params = {}
    if q:
        params["q"] = q
    if scope:
        params["scope"] = scope
    if source:
        params["source"] = source
    if params:
        extra = "&" + urlencode(params)
    filt = (
        "<form method='get' action='/admin/pairs'>"
        "<input type='text' name='q' placeholder='Search text' value='%s'> "
        "<input type='text' name='scope' placeholder='Scope (chat id or global)' value='%s'> "
        "<select name='source'><option value=''>All sources</option>%s</select> "
        "<button type='submit'>Filter</button></form>" % (
            html.escape(q, quote=True), html.escape(scope, quote=True),
            "".join(
                "<option value='%s'%s>%s</option>" % (
                    s_, " selected" if source == s_ else "", s_
                )
                for s_ in ("seed", "auto", "teach")
            ),
        )
    )
    add = (
        "<h2>Add a pair (global, like /teach)</h2>"
        "<form method='post' action='/admin/pairs/add'>"
        "<input type='text' name='stimulus' placeholder='Question / stimulus' required> "
        "<input type='text' name='response' placeholder='Answer / response' required> "
        "<input type='text' name='mood' placeholder='Mood (optional)' style='min-width:8rem'> "
        "<button type='submit'>Add pair</button></form>"
    )
    body = (
        filt + add +
        "<h2>Pairs</h2><table><tr><th>ID</th><th>Scope</th><th>Source</th>"
        "<th>Stimulus</th><th>Response</th><th>Mood</th><th>Hits</th><th></th></tr>" +
        "".join(trs) + "</table>" + _pager("/admin/pairs", page, total, extra)
    )
    return _page("Learned pairs", body)


@router.post("/pairs/add")
async def pairs_add(request: Request):
    _require(request)
    if not _authed(request):
        return _login_page()
    form = await _form(request)
    stimulus = (form.get("stimulus") or "").strip()
    response = (form.get("response") or "").strip()
    mood = (form.get("mood") or "").strip().lower() or None
    if stimulus and response:
        await pairs.learn(
            0, stimulus, response, source="teach", mood=mood,
        )
    return RedirectResponse("/admin/pairs", status_code=303)


@router.post("/pairs/delete")
async def pairs_delete(request: Request):
    _require(request)
    if not _authed(request):
        return _login_page()
    form = await _form(request)
    try:
        pair_id = int(form.get("id", ""))
    except ValueError:
        pair_id = 0
    if pair_id:
        await db.delete_pair(pair_id)
    return RedirectResponse("/admin/pairs", status_code=303)


# ----------------------------------------------------------------- users ----

@router.get("/users")
async def users_page(request: Request, page: int = 1):
    gate = _gate(request)
    if gate is not None:
        return gate
    page = max(1, page)
    rows, total = await db.recent_users(
        limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE
    )
    trs = []
    for r in rows:
        created = r["created_at"].strftime("%Y-%m-%d") if r.get("created_at") else ""
        trs.append(
            "<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%d</td><td>%d</td><td>%s</td></tr>" % (
                r["user_id"],
                html.escape(str(r["username"] or "")),
                html.escape(str(r["first_name"] or "")),
                html.escape(str(r["coins"])), html.escape(str(r["gems"])),
                int(r["xp"] or 0), int(r["kills"] or 0), created,
            )
        )
    body = (
        "<table><tr><th>User ID</th><th>Username</th><th>Name</th><th>Coins</th>"
        "<th>Gems</th><th>XP</th><th>Kills</th><th>Joined</th></tr>" +
        "".join(trs) + "</table>" + _pager("/admin/users", page, total)
    )
    return _page("Users", body)


# -------------------------------------------------------------- stickers ----

@router.get("/stickers")
async def stickers_page(request: Request):
    gate = _gate(request)
    if gate is not None:
        return gate
    pool = await db.sticker_pool_rows()
    ptrs = "".join(
        "<tr><td>%s</td><td><code>%s</code></td><td>%s</td></tr>" % (
            html.escape(str(r["mood"])),
            html.escape(str(r["file_id"])[:28] + "…"),
            html.escape(str(r["added_by"] or "")),
        )
        for r in pool
    ) or "<tr><td colspan='3' class='mut'>Pool is empty — seed stickers via DM.</td></tr>"
    harvested = await db.top_harvested_stickers(50)
    htrs = "".join(
        "<tr><td>%s</td><td>%s</td><td class='txt'>%s</td><td><code>%s</code></td>"
        "<td>%d</td></tr>" % (
            html.escape(str(r["scope"])), html.escape(str(r["kind"])),
            html.escape(str(r["stimulus_raw"] or r["stimulus_key"])[:80]),
            html.escape(str(r["file_id"])[:28] + "…"),
            int(r["hits"] or 0),
        )
        for r in harvested
    ) or "<tr><td colspan='5' class='mut'>Nothing harvested yet.</td></tr>"
    body = (
        "<h2>Owner sticker pool (by mood)</h2>"
        "<table><tr><th>Mood</th><th>file_id</th><th>Added by</th></tr>" +
        ptrs + "</table>"
        "<h2>Harvested sticker associations (top by hits)</h2>"
        "<table><tr><th>Scope</th><th>Kind</th><th>Stimulus</th>"
        "<th>file_id</th><th>Hits</th></tr>" + htrs + "</table>"
    )
    return _page("Stickers", body)
