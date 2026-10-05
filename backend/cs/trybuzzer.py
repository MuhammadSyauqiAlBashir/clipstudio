"""TryBuzzer (Indonesian clipping/UGC marketplace): the public bounty list goes into the Campaigns catalogue; with the
owner's own login (his Supabase session, saved in /etc/clipstudio/sessions/trybuzzer-auth.json, refreshed by the app)
Clip Studio submits his posted clip links the same way TryBuzzer's website does (owner's decision, 2026-10-05).
TryBuzzer has no "join" step: you submit links to a bounty. Bounties that need an analytics screen recording or an
application stay manual."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("cs.trybuzzer")

SITE = "https://www.trybuzzer.com"
SUPABASE = "https://owpvowbjiypyhgatunoh.supabase.co"
AUTH_FILE = Path(config.SESSIONS_DIR) / "trybuzzer-auth.json"
ANON_FILE = Path(config.SESSIONS_DIR) / "trybuzzer.anon"  # the site's public anon key (shipped to every visitor)


class TryBuzzerError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---- public catalogue ----------------------------------------------------------------------------------
async def fetch_bounties() -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(f"{SITE}/api/bounties/active", headers={"Accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    items = d if isinstance(d, list) else d.get("data") or []
    return [normalize(b) for b in items if b.get("id") and b.get("accepting_submissions", True)
            and not b.get("is_archived") and not b.get("is_private")]


def normalize(b: dict) -> dict:
    total = float(b.get("total_budget") or 0)
    used = float(b.get("claimed_amount") or 0)
    mentions = [m for m in (b.get("tiktok_mention"), b.get("instagram_mention"), b.get("threads_mention")) if m]
    lines = [f"Type: {b.get('content_type') or '-'} · payout: {b.get('payout_type') or '-'}"]
    if b.get("minimum_views") or b.get("min_views_for_payment"):
        lines.append(f"Minimum views: {b.get('minimum_views') or b.get('min_views_for_payment')}")
    if b.get("minimum_followers"):
        lines.append(f"Minimum followers: {b['minimum_followers']}")
    if b.get("mention_required") and mentions:
        lines.append("Mention required: " + " ".join(mentions))
    if b.get("deadline") or b.get("closes_at"):
        lines.append(f"Deadline: {(b.get('closes_at') or b.get('deadline'))[:10]}")
    if b.get("requires_application"):
        lines.append("Needs an application before submitting.")
    if b.get("guideline_url"):
        lines.append(f"Guidelines: {b['guideline_url']}")
    flags = []
    if "ugc" in f"{b.get('title', '')} {b.get('content_type', '')}".lower():  # UGC = film your own content
        flags.append("ugc")
    if b.get("requires_application"):
        flags.append("application")
    platforms = [p for p in (b.get("allowed_platforms") or [])]
    if not set(platforms) & {"tiktok", "instagram", "youtube", "facebook"}:
        flags.append("other_platforms")
    if total and used / total >= 0.9:
        flags.append("budget_low")
    hashtags = b.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = hashtags.split()
    return {"platform": "trybuzzer", "ext_id": str(b["id"]), "title": (b.get("title") or "")[:200], "creator": "",
            "rate": int(float(b.get("payment_per_1k_views") or 0)), "rate_unit": 1000, "platforms": platforms,
            "budget_used": round(used / total * 100, 1) if total else 0.0, "clippers": 0,
            "hashtags": [h if h.startswith("#") else f"#{h}" for h in hashtags if h][:20],
            "footage": [{"url": b["guideline_url"], "required": True, "text": "guidelines"}] if b.get("guideline_url") else [],
            "brief": "\n".join(lines), "image": b.get("image_url") or "", "url": f"{SITE}/bounty",
            "product_cart": False, "max_reward": int(float(b.get("reward_amount") or 0)),
            "start": b.get("created_at") or "", "end": b.get("closes_at") or b.get("deadline") or "", "flags": flags,
            "min_views": int(b.get("minimum_views") or b.get("min_views_for_payment") or 0)}


# ---- the owner's session -------------------------------------------------------------------------------
def _auth_file_exists() -> bool:
    try:
        return AUTH_FILE.exists()
    except OSError:  # unreadable sessions dir (e.g. not the app user): not connected
        return False


def configured() -> bool:
    return bool((db.kv_get("trybuzzer_auth", {}) or {}).get("refresh_token") or _auth_file_exists())


def _anon() -> str:
    try:
        return ANON_FILE.read_text().strip()
    except OSError:
        return ""


def _auth() -> dict:
    a = db.kv_get("trybuzzer_auth", {}) or {}
    if not a.get("refresh_token") and _auth_file_exists():  # first use: the session the owner copied from his browser
        raw = json.loads(AUTH_FILE.read_text())
        a = {"access_token": raw.get("access_token", ""), "refresh_token": raw.get("refresh_token", ""),
             "expires_at": float(raw.get("expires_at") or 0), "user_id": (raw.get("user") or {}).get("id", "")}
        db.kv_set("trybuzzer_auth", a)
    return a


async def access_token() -> str:
    a = _auth()
    if not a.get("refresh_token"):
        raise TryBuzzerError("TryBuzzer isn't connected.")
    if a.get("expires_at", 0) > time.time() + 300:
        return a["access_token"]
    async with httpx.AsyncClient(timeout=30) as http:  # Supabase rotates the refresh token on every use
        r = await http.post(f"{SUPABASE}/auth/v1/token", params={"grant_type": "refresh_token"},
                            json={"refresh_token": a["refresh_token"]}, headers={"apikey": _anon()})
    if r.status_code != 200:
        raise TryBuzzerError("TryBuzzer login expired — copy a fresh session (see the Campaigns page).", r.status_code)
    d = r.json()
    a.update(access_token=d["access_token"], refresh_token=d["refresh_token"],
             expires_at=float(d.get("expires_at") or time.time() + float(d.get("expires_in", 3600))))
    db.kv_set("trybuzzer_auth", a)
    return a["access_token"]


async def _call(method: str, path: str, json_body: dict | None = None) -> dict | list:
    tok = await access_token()
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request(method, f"{SITE}{path}", json=json_body, headers={
            "Authorization": f"Bearer {tok}", "X-Buzzer-Client": "web", "Accept": "application/json"})
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code in (401, 403):
        raise TryBuzzerError("TryBuzzer refused the login — copy a fresh session.", r.status_code)
    if r.status_code >= 400:
        raise TryBuzzerError(f"TryBuzzer {r.status_code}: {(d.get('error') if isinstance(d, dict) else '') or r.text[:200]}",
                             r.status_code)
    return d


async def social_accounts() -> list[dict]:
    d = await _call("GET", "/api/social-accounts")
    return d if isinstance(d, list) else (d.get("data") or d.get("accounts") or [])


async def submit(bounty_id: str, url: str, platform: str, account: dict, views: int, description: str = "") -> dict:
    """The same submission TryBuzzer's own form sends."""
    return await _call("POST", "/api/submissions", {
        "bounty_id": bounty_id, "content_url": url, "description": description[:500], "platform": platform,
        "social_account_id": account.get("id"), "submitted_username": account.get("username") or account.get("handle") or "",
        "username_verified": True, "submitted_view_count": int(views), "content_format": ""})
