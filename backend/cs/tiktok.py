"""TikTok: Login Kit (connect the owner's own account) + Content Posting API "upload to inbox" (video.upload):
the approved final lands in the TikTok inbox as a draft, and the owner finishes the post in the TikTok app.
Direct posting would need TikTok's audit. Tokens live in Clip Studio's DB (access 24 h, refresh 365 days)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import config, db

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.upload"
CHUNK = 10 * 1024 * 1024


class TikTokError(Exception):
    pass


def configured() -> bool:
    return bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET)


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/api/tiktok/oauth"


def auth() -> dict:
    return db.kv_get("tiktok_auth", {}) or {}


def connected() -> bool:
    return configured() and bool(auth().get("refresh_token"))


def authorize_url() -> str:
    """Start the login. The state is single-use and expires in 10 minutes (the callback has no session cookie:
    SameSite=Strict cookies aren't sent when TikTok redirects back)."""
    state = secrets.token_urlsafe(24)
    db.kv_set("tiktok_state", {"state": state, "exp": time.time() + 600})
    return AUTH_URL + "?" + urlencode({"client_key": config.TIKTOK_CLIENT_KEY, "scope": SCOPES, "response_type": "code",
                                       "redirect_uri": redirect_uri(), "state": state})


def check_state(state: str) -> bool:
    saved = db.kv_get("tiktok_state", {}) or {}
    db.kv_set("tiktok_state", {})
    return bool(state) and saved.get("exp", 0) > time.time() and secrets.compare_digest(saved.get("state", ""), state)


async def _token(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{API}/oauth/token/", data={"client_key": config.TIKTOK_CLIENT_KEY,
                                                         "client_secret": config.TIKTOK_CLIENT_SECRET, **data})
    d = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in d:
        raise TikTokError(f"TikTok login: {d.get('error_description') or d.get('error') or r.status_code}")
    return d


def _save(d: dict, extra: dict | None = None):
    now = time.time()
    db.kv_set("tiktok_auth", {**auth(), **(extra or {}), "access_token": d["access_token"],
                              "expires": now + float(d.get("expires_in", 86400)),
                              "refresh_token": d.get("refresh_token") or auth().get("refresh_token", ""),
                              "refresh_expires": now + float(d.get("refresh_expires_in", 365 * 86400)),
                              "open_id": d.get("open_id") or auth().get("open_id", ""), "scope": d.get("scope", "")})


async def finish_login(code: str):
    d = await _token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    _save(d)
    try:
        info = await user_info()
        _save(d, {"display_name": info.get("display_name", ""), "avatar": info.get("avatar_url", "")})
    except TikTokError:
        pass


async def access_token() -> str:
    a = auth()
    if not a.get("refresh_token"):
        raise TikTokError("TikTok isn't connected.")
    if a.get("expires", 0) < time.time() + 600:
        d = await _token({"grant_type": "refresh_token", "refresh_token": a["refresh_token"]})
        _save(d)
        a = auth()
    return a["access_token"]


async def _api(method: str, path: str, **kw) -> dict:
    tok = await access_token()
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {tok}",
                                                                "Content-Type": "application/json; charset=UTF-8"}, **kw)
    d = r.json() if r.content else {}
    err = d.get("error") or {}
    if r.status_code != 200 or (err.get("code") not in (None, "", "ok")):
        raise TikTokError(f"TikTok {r.status_code}: {err.get('message') or err.get('code') or r.text[:200]}")
    return d.get("data") or {}


async def user_info() -> dict:
    return (await _api("GET", "/user/info/", params={"fields": "open_id,avatar_url,display_name"})).get("user") or {}


async def disconnect():
    a = auth()
    if a.get("access_token"):
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                await http.post(f"{API}/oauth/revoke/", data={"client_key": config.TIKTOK_CLIENT_KEY,
                                                              "client_secret": config.TIKTOK_CLIENT_SECRET,
                                                              "token": a["access_token"]})
        except httpx.HTTPError:
            pass
    db.kv_set("tiktok_auth", {})


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) by TikTok's rules: up to 64 MB in one piece, otherwise 10 MB chunks with the
    remainder merged into the last chunk."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    return CHUNK, size // CHUNK


async def upload_draft(path: Path) -> str:
    """Send the video to the owner's TikTok inbox. Returns the publish_id."""
    data = path.read_bytes()
    size = len(data)
    chunk, count = chunk_plan(size)
    init = await _api("POST", "/post/publish/inbox/video/init/", json={"source_info": {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
    url, publish_id = init["upload_url"], init["publish_id"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as http:
        for i in range(count):
            start = i * chunk
            end = size if i == count - 1 else start + chunk
            r = await http.put(url, content=data[start:end], headers={
                "Content-Type": "video/mp4", "Content-Length": str(end - start),
                "Content-Range": f"bytes {start}-{end - 1}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"TikTok upload {r.status_code}: {r.text[:200]}")
    return publish_id


async def status(publish_id: str) -> tuple[str, str]:
    d = await _api("POST", "/post/publish/status/fetch/", json={"publish_id": publish_id})
    return d.get("status", ""), d.get("fail_reason", "")
