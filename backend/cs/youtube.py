"""YouTube Shorts upload for the owner's own channel: Google OAuth (scope youtube.upload, offline access) + the
resumable videos.insert upload (1,600 quota units each, from the clipstudio-ai project's 10,000 a day).
Until Google's API audit passes, YouTube locks API uploads as private — so auto-posting to YouTube stays off by
default; connecting + a test upload are what the audit asks to see."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import config, db

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
UPLOAD_UNITS = 1600
DAILY_UPLOADS = 5  # 5 × 1,600 = 8,000 units; the watchers keep the rest of the 10,000


class YouTubeError(Exception):
    pass


def configured() -> bool:
    return bool(config.YT_OAUTH_CLIENT_ID and config.YT_OAUTH_CLIENT_SECRET)


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/api/youtube/oauth"


def auth() -> dict:
    return db.kv_get("youtube_auth", {}) or {}


def connected() -> bool:
    return configured() and bool(auth().get("refresh_token"))


def authorize_url() -> str:
    state = secrets.token_urlsafe(24)
    db.kv_set("youtube_state", {"state": state, "exp": time.time() + 600})
    return AUTH_URL + "?" + urlencode({"client_id": config.YT_OAUTH_CLIENT_ID, "redirect_uri": redirect_uri(),
                                       "response_type": "code", "scope": SCOPE, "access_type": "offline",
                                       "prompt": "consent", "include_granted_scopes": "true", "state": state})


def check_state(state: str) -> bool:
    saved = db.kv_get("youtube_state", {}) or {}
    db.kv_set("youtube_state", {})
    return bool(state) and saved.get("exp", 0) > time.time() and secrets.compare_digest(saved.get("state", ""), state)


async def _token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(TOKEN_URL, data={"client_id": config.YT_OAUTH_CLIENT_ID,
                                             "client_secret": config.YT_OAUTH_CLIENT_SECRET, **data})
    d = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in d:
        raise YouTubeError(f"Google login: {d.get('error_description') or d.get('error') or r.status_code}")
    return d


def _save(d: dict):
    a = auth()
    db.kv_set("youtube_auth", {**a, "access_token": d["access_token"], "expires": time.time() + float(d.get("expires_in", 3600)),
                               "refresh_token": d.get("refresh_token") or a.get("refresh_token", ""),
                               "scope": d.get("scope", a.get("scope", ""))})


async def finish_login(code: str):
    d = await _token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    if not d.get("refresh_token"):
        raise YouTubeError("Google didn't give a long-term login; remove the app at myaccount.google.com/permissions "
                           "and connect again.")
    _save(d)


async def access_token() -> str:
    a = auth()
    if not a.get("refresh_token"):
        raise YouTubeError("YouTube isn't connected.")
    if a.get("expires", 0) < time.time() + 300:
        _save(await _token({"grant_type": "refresh_token", "refresh_token": a["refresh_token"]}))
        a = auth()
    return a["access_token"]


async def disconnect():
    a = auth()
    tok = a.get("refresh_token") or a.get("access_token")
    if tok:
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                await http.post("https://oauth2.googleapis.com/revoke", data={"token": tok})
        except httpx.HTTPError:
            pass
    db.kv_set("youtube_auth", {})


def uploads_left() -> int:
    return max(0, DAILY_UPLOADS - int(db.usage_get("yt_uploads")))


async def upload(path: Path, title: str, description: str, tags: list[str], language: str = "") -> dict:
    """Resumable upload of one Short. Returns {id, url, privacy, upload_status, channel}."""
    if uploads_left() <= 0:
        raise YouTubeError(f"Daily YouTube upload limit reached ({DAILY_UPLOADS}/day keeps the free quota safe)")
    data = path.read_bytes()
    meta = {"snippet": {"title": title[:100] or "Clip", "description": description[:4900],
                        "tags": [t.lstrip("#")[:30] for t in tags][:15], "categoryId": "24"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "embeddable": True}}
    if language:
        meta["snippet"]["defaultLanguage"] = meta["snippet"]["defaultAudioLanguage"] = language
    tok = await access_token()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20)) as http:
        r = await http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                            headers={"Authorization": f"Bearer {tok}", "X-Upload-Content-Type": "video/mp4",
                                     "X-Upload-Content-Length": str(len(data))})
        if r.status_code != 200 or "location" not in r.headers:
            raise YouTubeError(f"YouTube {r.status_code}: {r.text[:300]}")
        db.usage_add("yt_units", UPLOAD_UNITS)
        db.usage_add("yt_uploads", 1)
        r = await http.put(r.headers["location"], content=data, headers={"Content-Type": "video/mp4"})
    if r.status_code not in (200, 201):
        raise YouTubeError(f"YouTube upload {r.status_code}: {r.text[:300]}")
    v = r.json()
    st, sn = v.get("status") or {}, v.get("snippet") or {}
    db.kv_set("youtube_auth", {**auth(), "channel": sn.get("channelTitle", ""), "channel_id": sn.get("channelId", "")})
    return {"id": v["id"], "url": f"https://www.youtube.com/shorts/{v['id']}", "privacy": st.get("privacyStatus", ""),
            "upload_status": st.get("uploadStatus", ""), "channel": sn.get("channelTitle", "")}
