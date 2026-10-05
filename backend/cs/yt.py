"""YouTube watching without spending quota on detection: WebSub push + RSS safety poll; then one
`videos.list` call (1 unit for up to 50 ids) to tell uploads from live/upcoming streams. Never search.list."""

from __future__ import annotations

import hashlib
import hmac
import logging
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

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
    if db.usage_get("yt_units", db.google_day()) >= config.YT_DAILY_UNITS:
        raise YTError("YouTube API quota for today is used up (resets 14:00 WIB)")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/{path}", params={**params, "key": config.YT_API_KEY})
    db.usage_add("yt_units", 1, db.google_day())
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
        data = await _api("videos", {"part": "snippet,contentDetails,liveStreamingDetails,statistics",
                                     "id": ",".join(ids[i:i + 50])})
        for it in data.get("items") or []:
            sn, ls = it.get("snippet", {}), it.get("liveStreamingDetails") or {}
            out.append({"id": it["id"], "title": sn.get("title", ""), "channel_id": sn.get("channelId", ""),
                        "channel_title": sn.get("channelTitle", ""), "published": sn.get("publishedAt", ""),
                        "state": sn.get("liveBroadcastContent", "none"),  # none / live / upcoming
                        "scheduled": ls.get("scheduledStartTime", ""), "ended": bool(ls.get("actualEndTime")),
                        "duration": iso_duration(it.get("contentDetails", {}).get("duration", "")),
                        "views": int((it.get("statistics") or {}).get("viewCount") or 0),
                        "thumb": _thumb(sn.get("thumbnails") or {})})
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


# ---- browsing a channel's uploads (Browse page) -------------------------------------------------------
def _thumb(t: dict) -> str:
    for k in ("medium", "high", "default"):
        if t.get(k, {}).get("url"):
            return t[k]["url"]
    return ""


def video_id_of(text: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", text)
    return m.group(1) if m else ""


PAGE_SIZE = 24
TABS = {"videos": "videos", "streams": "streams"}


async def _flat(url: str, first: int, last: int) -> dict:
    """yt-dlp's flat listing of a channel tab or a search: no API quota (reads YouTube's own pages)."""
    from . import fetch  # local import: fetch is only needed for Browse
    work = config.STATE_DIR / "cache" / "browse"
    work.mkdir(parents=True, exist_ok=True)
    out = await fetch.ytdlp(["-J", "--flat-playlist", "--playlist-items", f"{first}:{last}", url], work, timeout=120)
    start = out.find("{")
    try:
        return json.loads(out[start:out.rfind("}") + 1])
    except ValueError as e:
        raise YTError("YouTube didn't return the channel page") from e


def _https(u: str) -> str:
    return "https:" + u if u.startswith("//") else u


def _avatar(thumbs: list) -> str:
    for t in reversed(thumbs or []):
        if "avatar" in str(t.get("id", "")) or (t.get("width") and t.get("width") == t.get("height")):
            return _https(t.get("url", ""))
    return _https((thumbs or [{}])[-1].get("url", "")) if thumbs else ""


def channel_url(channel_id_or_handle: str) -> str:
    t = channel_id_or_handle.strip()
    return f"https://www.youtube.com/channel/{t}" if re.fullmatch(r"UC[\w-]{22}", t) else f"https://www.youtube.com/@{t.lstrip('@')}"


async def find_channels(text: str) -> tuple[str, list[dict]]:
    """A handle, channel link, channel id or video link → (channel id or @handle, []); a plain name → ("", matches).
    Everything goes through yt-dlp, so looking up channels costs no API quota."""
    t = text.strip()
    vid = video_id_of(t)
    if vid:
        from . import fetch
        meta = await fetch.info(f"https://www.youtube.com/watch?v={vid}", config.STATE_DIR / "cache" / "browse")
        return meta["channel_id"] or meta["creator_url"], []
    m = re.search(r"(UC[\w-]{22})", t)
    if m:
        return m.group(1), []
    h = re.search(r"@([\w.\-]+)", t)
    if h:
        return "@" + h.group(1), []
    if "youtube.com/" in t:
        return t.rstrip("/").rsplit("/", 1)[-1], []
    if re.fullmatch(r"[\w.\-]{3,30}", t):  # maybe a handle typed without @
        try:
            d = await _flat(channel_url(t) + "/videos", 1, 1)
            if d.get("channel_id"):
                return d["channel_id"], []
        except Exception:  # noqa: BLE001 - not a handle: search by name below
            pass
    d = await _flat("https://www.youtube.com/results?" + urlencode({"search_query": t, "sp": "EgIQAg=="}), 1, 6)
    out = [{"id": e.get("id", ""), "title": e.get("title", ""), "thumb": _https((e.get("thumbnails") or [{}])[-1].get("url", "")),
            "description": f"{int(e.get('channel_follower_count') or 0):,} subscribers"}
           for e in d.get("entries") or [] if str(e.get("id", "")).startswith("UC")]
    return "", out


async def uploads_page(channel: str, page: str = "", tab: str = "videos") -> dict:
    """One page (24) of a channel's videos or live replays, newest first, via yt-dlp (0 API units).
    `page` is the page number as text ("" = 1). Results are cached 10 minutes."""
    n = max(1, int(page or 1))
    key = f"browse:{channel}:{tab}:{n}"
    hit = db.kv_get(key)
    if hit and hit.get("at", 0) > time.time() - 600:
        return hit["data"]
    first = (n - 1) * PAGE_SIZE + 1
    from .fetch import FetchError
    try:
        d = await _flat(channel_url(channel) + "/" + TABS.get(tab, "videos"), first, first + PAGE_SIZE - 1)
    except FetchError as e:
        if "does not have a" not in str(e) or tab == "videos":
            raise
        d = await _flat(channel_url(channel) + "/videos", 1, 1)  # channel header only: this tab is empty
        d["entries"] = []
    cid = d.get("channel_id") or channel
    handle = (d.get("uploader_id") or "").lstrip("@")
    ch = {"id": cid, "title": d.get("channel") or d.get("uploader") or "", "handle": handle,
          "thumb": _avatar(d.get("thumbnails") or []), "subscribers": int(d.get("channel_follower_count") or 0),
          "video_count": 0, "url": f"https://www.youtube.com/@{handle}" if handle else channel_url(cid)}
    vids = []
    for e in d.get("entries") or []:
        if not e.get("id"):
            continue
        vids.append({"id": e["id"], "title": e.get("title", ""), "duration": float(e.get("duration") or 0),
                     "views": int(e.get("view_count") or 0), "state": "live" if e.get("live_status") == "is_live" else
                     "upcoming" if e.get("live_status") == "is_upcoming" else "none",
                     "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg", "published": "", "channel_id": cid})
    data = {"channel": ch, "videos": vids, "next": str(n + 1) if len(vids) == PAGE_SIZE else "",
            "prev": str(n - 1) if n > 1 else "", "total": 0}
    db.kv_set(key, {"at": time.time(), "data": data})
    return data
