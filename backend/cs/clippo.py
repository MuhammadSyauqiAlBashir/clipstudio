"""Clippo as the owner's own account (owner's decision, 2026-10-05): join campaigns and submit posted clips through
the same requests Clippo's website makes (app.clippo.id/api/proxy/…), using the owner's browser session, saved as a
copied request in /etc/clipstudio/sessions/clippo.curl (root:clipstudio 640, never printed).
When the session expires, calls fail with 401 and the owner is asked to copy a fresh request."""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("cs.clippo")

BASE = "https://app.clippo.id/api/proxy/"
SESSION_FILE = Path(config.SESSIONS_DIR) / "clippo.curl"


class ClippoError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _session() -> dict:
    """Headers (Cookie + browser user agent) from the copied request. Missing file → {}."""
    try:
        raw = SESSION_FILE.read_text()
    except OSError:
        return {}
    parts = shlex.split(raw.replace("\\\n", " "))
    headers: dict[str, str] = {}
    for i, p in enumerate(parts):
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        if p in ("-H", "--header"):
            k, _, v = nxt.partition(":")
            if k.strip().lower() not in ("if-none-match", "content-length", "host", "content-type"):
                headers[k.strip()] = v.strip()  # the browser's own headers (Cloudflare checks some of them)
        elif p in ("-b", "--cookie"):
            headers["Cookie"] = nxt
    return headers if "Cookie" in headers else {}


def configured() -> bool:
    return bool(_session())


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    headers = _session()
    if not headers:
        raise ClippoError("Clippo isn't connected (no saved session).")
    if method != "GET":
        headers.update({"Origin": "https://app.clippo.id", "Content-Type": "application/json"})
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request(method, BASE + path, json=json, headers=headers)
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code in (401, 403):
        raise ClippoError("Clippo login expired — copy a fresh request (see the Campaigns page).", r.status_code)
    if r.status_code >= 400 or (isinstance(d, dict) and int(d.get("status") or 200) >= 400):
        raise ClippoError(f"Clippo {r.status_code}: {(d.get('message') if isinstance(d, dict) else '') or r.text[:200]}",
                          r.status_code)
    return d


async def stats() -> dict:
    return ((await _call("GET", "clips/clipper/stats")).get("data")) or {}


async def joined_ids() -> set[str]:
    d = (await _call("GET", "clips/clipper/campaigns", None)).get("data") or {}
    items = d.get("data") if isinstance(d, dict) else d
    out = set()
    for c in items or []:
        cid = c.get("campaignId") or (c.get("campaign") or {}).get("id") or c.get("id")
        if cid:
            out.add(str(cid))
    return out


async def join(campaign_id: str) -> dict:
    return await _call("POST", "clips/clipper/campaign/join", {"campaignId": campaign_id})


async def bulk_check(campaign_id: str, urls: list[str]) -> list[dict]:
    """Clippo looks each link up (platform, caption, date, views) and says whether it's eligible for the campaign."""
    d = await _call("POST", "clips/clipper/bulk-check", {"campaignId": campaign_id,
                                                         "videoUrls": [{"url": u} for u in urls]})
    data = d.get("data")
    return data if isinstance(data, list) else [data] if data else []


def submission_item(url: str, checked: dict) -> dict:
    """The clip entry Clippo's own submit form builds from a check result."""
    data = checked.get("data") or {}
    video = data.get("video") or {}
    platform = data.get("platform")
    return {"videoUrl": url, "analyticsVideoUrl": "", "platform": platform,
            "clipTitle": data.get("caption") or data.get("description") or "",
            "datePosted": data.get("postedAt") or "",
            "thumbnailUrl": (video.get("cover") if str(platform).upper() == "TIKTOK" else video.get("thumbnailUrl")) or ""}


def eligible(checked: dict) -> bool:
    return bool(checked.get("success") and ((checked.get("data") or {}).get("eligibility") or {}).get("eligible"))


def reason(checked: dict) -> str:
    if eligible(checked):
        return ""
    elig = (checked.get("data") or {}).get("eligibility") or {}
    return str(checked.get("error") or elig.get("reason") or elig.get("message") or checked.get("errorCode") or "not eligible yet")[:200]


async def submit(campaign_id: str, items: list[dict]) -> dict:
    return await _call("POST", "clips/clipper/submit-batch", {"campaignId": campaign_id, "clips": items})
