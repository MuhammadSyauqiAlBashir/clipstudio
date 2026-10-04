"""YouTube watching without spending quota on detection: WebSub push + RSS safety poll; then one
`videos.list` call (1 unit for up to 50 ids) to tell uploads from live/upcoming streams. Never search.list."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from . import config, db

log = logging.getLogger("cs.yt")

API = "https://www.googleapis.com/youtube/v3"
HUB = "https://pubsubhubbub.appspot.com/subscribe"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
LEASE = 5 * 86400


class YTError(Exception):
    pass


def secret() -> str:
    s = config.WEBHOOK_SECRET or db.kv_get("webhook_secret")
    if not s:
        import secrets as _s
        s = _s.token_hex(32)
        db.kv_set("webhook_secret", s)
    return s


def channel_secret(ext_id: str) -> str:
    return hmac.new(secret().encode(), f"yt:{ext_id}".encode(), hashlib.sha256).hexdigest()[:48]


def topic(ext_id: str) -> str:
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={ext_id}"


def verify_signature(ext_id: str, body: bytes, header: str) -> bool:
    """WebSub hubs sign with HMAC-SHA1: X-Hub-Signature: sha1=<hex>."""
    if not header or "=" not in header:
        return False
    algo, sig = header.split("=", 1)
    digest = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}.get(algo.lower())
    if not digest:
        return False
    good = hmac.new(channel_secret(ext_id).encode(), body, digest).hexdigest()
    return hmac.compare_digest(good, sig.strip().lower())


def parse_atom(body: bytes | str) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if vid:
            out.append({"video_id": vid, "channel_id": e.findtext("yt:channelId", "", NS),
                        "title": e.findtext("a:title", "", NS), "published": e.findtext("a:published", "", NS)})
    return out


def iso_duration(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + se)


async def _api(path: str, params: dict) -> dict:
    if not config.YT_API_KEY:
        raise YTError("YT_API_KEY is not set.")
    if db.usage_get("yt_units") >= config.YT_DAILY_UNITS:
        raise YTError("YouTube daily unit budget used")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/{path}", params={**params, "key": config.YT_API_KEY})
    db.usage_add("yt_units", 1)
    if r.status_code != 200:
        raise YTError(f"YouTube API {r.status_code}: {r.text[:200]}")
    return r.json()


async def resolve(text: str) -> dict:
    """@handle / channel URL / UC… id → {ext_id, title, handle, url}."""
    t = text.strip()
    m = re.search(r"(UC[\w-]{22})", t)
    if m:
        params = {"part": "snippet", "id": m.group(1)}
    else:
        h = re.search(r"@([\w.\-]+)", t)
        handle = h.group(1) if h else t.rsplit("/", 1)[-1]
        params = {"part": "snippet", "forHandle": "@" + handle}
    items = (await _api("channels", params)).get("items") or []
    if not items:
        raise YTError("YouTube channel not found")
    it = items[0]
    handle = (it["snippet"].get("customUrl") or "").lstrip("@")
    return {"ext_id": it["id"], "title": it["snippet"]["title"], "handle": handle,
            "url": f"https://www.youtube.com/@{handle}" if handle else f"https://www.youtube.com/channel/{it['id']}"}


async def videos(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        data = await _api("videos", {"part": "snippet,contentDetails,liveStreamingDetails", "id": ",".join(ids[i:i + 50])})
        for it in data.get("items") or []:
            sn, ls = it.get("snippet", {}), it.get("liveStreamingDetails") or {}
            out.append({"id": it["id"], "title": sn.get("title", ""), "channel_id": sn.get("channelId", ""),
                        "channel_title": sn.get("channelTitle", ""), "published": sn.get("publishedAt", ""),
                        "state": sn.get("liveBroadcastContent", "none"),  # none / live / upcoming
                        "scheduled": ls.get("scheduledStartTime", ""), "ended": bool(ls.get("actualEndTime")),
                        "duration": iso_duration(it.get("contentDetails", {}).get("duration", ""))})
    return out


async def rss(ext_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        r = await http.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={ext_id}")
    if r.status_code != 200:
        raise YTError(f"RSS {r.status_code}")
    return parse_atom(r.content)


async def subscribe(row_id: int, ext_id: str, mode: str = "subscribe") -> bool:
    data = {"hub.callback": f"{config.PUBLIC_URL}/api/websub/callback?c={row_id}", "hub.topic": topic(ext_id),
            "hub.verify": "async", "hub.mode": mode, "hub.lease_seconds": str(LEASE), "hub.secret": channel_secret(ext_id)}
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(HUB, data=data)
    if r.status_code not in (202, 204):
        log.warning("websub %s %s: %s %s", mode, ext_id, r.status_code, r.text[:200])
        return False
    return True
