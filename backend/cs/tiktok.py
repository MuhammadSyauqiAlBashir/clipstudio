"""TikTok: Login Kit (connect the owner's own account) + Content Posting API.

Two ways to post:
- "upload to inbox" (video.upload, approved): the final lands in the TikTok inbox as a draft and the owner finishes
  the post in the TikTok app;
- Direct Post (video.publish): straight to the profile. TikTok requires the owner to choose, for every post, who can
  see it (no default), comment/duet/stitch and the commercial-content disclosure, and to see whose account it goes
  to — so Direct Post is never automatic: the owner taps "Post to TikTok" and confirms. Until TikTok approves
  video.publish for an app, it only allows private ("only me") posts.
video.list reads the owner's public videos (views, likes) for the Stats tab.

Two TikTok apps: "production" (approved) and "sandbox" (the same app's test side, used to build and record the demo
before TikTok approves new permissions). Each keeps its own login (DB kv tiktok_auth / tiktok_auth_sandbox)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

from . import config, db

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES_INBOX = "user.info.basic,video.upload"
SCOPES_FULL = "user.info.basic,video.upload,video.publish,video.list"
CHUNK = 10 * 1024 * 1024
PRIVACY_LABELS = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
                  "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}


class TikTokError(Exception):
    pass


# ---- which app -----------------------------------------------------------------------------------------
def app_name() -> str:
    return "sandbox" if db.settings().get("tiktok_app") == "sandbox" else "production"


def keys(app: str | None = None) -> tuple[str, str]:
    if (app or app_name()) == "sandbox":
        return config.TIKTOK_SANDBOX_CLIENT_KEY, config.TIKTOK_SANDBOX_CLIENT_SECRET
    return config.TIKTOK_CLIENT_KEY, config.TIKTOK_CLIENT_SECRET


def _kv(app: str | None = None) -> str:
    return "tiktok_auth_sandbox" if (app or app_name()) == "sandbox" else "tiktok_auth"


def scopes(app: str | None = None) -> str:
    """The sandbox always asks for everything; production only for what TikTok has approved there."""
    if (app or app_name()) == "sandbox" or db.settings().get("tiktok_direct_approved"):
        return SCOPES_FULL
    return SCOPES_INBOX


def configured(app: str | None = None) -> bool:
    return all(keys(app))


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/api/tiktok/oauth"


def auth(app: str | None = None) -> dict:
    return db.kv_get(_kv(app), {}) or {}


def connected(app: str | None = None) -> bool:
    return configured(app) and bool(auth(app).get("refresh_token"))


def has_scope(name: str, app: str | None = None) -> bool:
    return name in (auth(app).get("scope") or "").replace(" ", "").split(",")


def direct_mode() -> bool:
    """Direct Post is on: the owner chose it and the connected login may post (video.publish)."""
    return bool(db.settings().get("tiktok_direct")) and connected() and has_scope("video.publish")


def private_only() -> bool:
    """TikTok allows only "only me" posts until it approves video.publish for this app (always so in the sandbox)."""
    return app_name() == "sandbox" or not db.settings().get("tiktok_direct_approved")


# ---- login ---------------------------------------------------------------------------------------------
def authorize_url() -> str:
    """Start the login. The state is single-use, expires in 10 minutes and remembers which app is logging in (the
    callback has no session cookie: SameSite=Strict cookies aren't sent when TikTok redirects back)."""
    app = app_name()
    state = secrets.token_urlsafe(24)
    db.kv_set("tiktok_state", {"state": state, "exp": time.time() + 600, "app": app})
    return AUTH_URL + "?" + urlencode({"client_key": keys(app)[0], "scope": scopes(app), "response_type": "code",
                                       "redirect_uri": redirect_uri(), "state": state})


def check_state(state: str) -> str:
    """The app the login was started for, or "" if the state is wrong or expired."""
    saved = db.kv_get("tiktok_state", {}) or {}
    db.kv_set("tiktok_state", {})
    ok = bool(state) and saved.get("exp", 0) > time.time() and secrets.compare_digest(saved.get("state", ""), state)
    return (saved.get("app") or "production") if ok else ""


async def _token(data: dict, app: str) -> dict:
    key, secret = keys(app)
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{API}/oauth/token/", data={"client_key": key, "client_secret": secret, **data})
    d = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in d:
        raise TikTokError(f"TikTok login: {d.get('error_description') or d.get('error') or r.status_code}")
    return d


def _save(d: dict, app: str, extra: dict | None = None):
    now = time.time()
    a = auth(app)
    db.kv_set(_kv(app), {**a, **(extra or {}), "access_token": d["access_token"],
                         "expires": now + float(d.get("expires_in", 86400)),
                         "refresh_token": d.get("refresh_token") or a.get("refresh_token", ""),
                         "refresh_expires": now + float(d.get("refresh_expires_in", 365 * 86400)),
                         "open_id": d.get("open_id") or a.get("open_id", ""), "scope": d.get("scope", "")})


async def finish_login(code: str, app: str | None = None):
    app = app or app_name()
    d = await _token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()}, app)
    _save(d, app)
    try:
        info = await user_info(app)
        _save(d, app, {"display_name": info.get("display_name", ""), "avatar": info.get("avatar_url", "")})
    except TikTokError:
        pass


async def access_token(app: str | None = None) -> str:
    app = app or app_name()
    a = auth(app)
    if not a.get("refresh_token"):
        raise TikTokError("TikTok isn't connected.")
    if a.get("expires", 0) < time.time() + 600:
        _save(await _token({"grant_type": "refresh_token", "refresh_token": a["refresh_token"]}, app), app)
        a = auth(app)
    return a["access_token"]


async def _api(method: str, path: str, app: str | None = None, **kw) -> dict:
    tok = await access_token(app)
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {tok}",
                                                                "Content-Type": "application/json; charset=UTF-8"}, **kw)
    d = r.json() if r.content else {}
    err = d.get("error") or {}
    if r.status_code != 200 or (err.get("code") not in (None, "", "ok")):
        raise TikTokError(f"TikTok {r.status_code}: {err.get('code') or ''} {err.get('message') or r.text[:200]}".strip())
    return d.get("data") or {}


async def user_info(app: str | None = None) -> dict:
    return (await _api("GET", "/user/info/", app, params={"fields": "open_id,avatar_url,display_name"})).get("user") or {}


async def disconnect():
    a = auth()
    key, secret = keys()
    if a.get("access_token"):
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                await http.post(f"{API}/oauth/revoke/", data={"client_key": key, "client_secret": secret,
                                                              "token": a["access_token"]})
        except httpx.HTTPError:
            pass
    db.kv_set(_kv(), {})


# ---- uploading -----------------------------------------------------------------------------------------
def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) by TikTok's rules: up to 64 MB in one piece, otherwise 10 MB chunks with the
    remainder merged into the last chunk."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    return CHUNK, size // CHUNK


async def _put_chunks(url: str, data: bytes, chunk: int, count: int):
    size = len(data)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as http:
        for i in range(count):
            start = i * chunk
            end = size if i == count - 1 else start + chunk
            r = await http.put(url, content=data[start:end], headers={
                "Content-Type": "video/mp4", "Content-Length": str(end - start),
                "Content-Range": f"bytes {start}-{end - 1}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"TikTok upload {r.status_code}: {r.text[:200]}")


async def upload_draft(path: Path) -> str:
    """Send the video to the owner's TikTok inbox. Returns the publish_id."""
    data = path.read_bytes()
    chunk, count = chunk_plan(len(data))
    init = await _api("POST", "/post/publish/inbox/video/init/", json={"source_info": {
        "source": "FILE_UPLOAD", "video_size": len(data), "chunk_size": chunk, "total_chunk_count": count}})
    await _put_chunks(init["upload_url"], data, chunk, count)
    return init["publish_id"]


async def creator_info() -> dict:
    """Whose account a Direct Post goes to and what it allows (TikTok asks apps to show this before posting)."""
    return await _api("POST", "/post/publish/creator_info/query/", json={})


def post_info(opts: dict) -> dict:
    """The owner's choices → TikTok's post_info."""
    return {"title": str(opts.get("title") or "")[:2200], "privacy_level": opts["privacy_level"],
            "disable_comment": not opts.get("allow_comment"), "disable_duet": not opts.get("allow_duet"),
            "disable_stitch": not opts.get("allow_stitch"), "video_cover_timestamp_ms": 1000,
            "brand_content_toggle": bool(opts.get("disclose") and opts.get("branded")),
            "brand_organic_toggle": bool(opts.get("disclose") and opts.get("your_brand"))}


async def direct_post(path: Path, opts: dict) -> str:
    """Post straight to the owner's profile with the choices he made. Returns the publish_id."""
    data = path.read_bytes()
    chunk, count = chunk_plan(len(data))
    init = await _api("POST", "/post/publish/video/init/", json={"post_info": post_info(opts), "source_info": {
        "source": "FILE_UPLOAD", "video_size": len(data), "chunk_size": chunk, "total_chunk_count": count}})
    await _put_chunks(init["upload_url"], data, chunk, count)
    return init["publish_id"]


async def status(publish_id: str) -> tuple[str, str]:
    d = await status_full(publish_id)
    return d.get("status", ""), d.get("fail_reason", "")


async def status_full(publish_id: str) -> dict:
    """status + fail_reason + publicaly_available_post_id (TikTok's spelling: the public video ids, if any)."""
    return await _api("POST", "/post/publish/status/fetch/", json={"publish_id": publish_id})


# ---- reading the owner's videos (video.list) -----------------------------------------------------------
async def my_videos(pages: int = 5) -> list[dict]:
    """The owner's public videos, newest first, with their numbers (20 per page)."""
    out, cursor = [], None
    fields = "id,title,create_time,share_url,view_count,like_count,comment_count,share_count"
    for _ in range(pages):
        body = {"max_count": 20, **({"cursor": cursor} if cursor else {})}
        d = await _api("POST", "/video/list/", params={"fields": fields}, json=body)
        out += d.get("videos") or []
        if not d.get("has_more"):
            break
        cursor = d.get("cursor")
    return out


def video_id(url: str) -> str:
    """The video id in a TikTok link (…/video/<id>), or ""."""
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    return parts[parts.index("video") + 1] if "video" in parts and parts.index("video") + 1 < len(parts) else ""


AVATAR_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "ttwstatic.com", "ibyteimg.com")


def avatar_allowed(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    return urlparse(url).scheme == "https" and any(host == h or host.endswith("." + h) for h in AVATAR_HOSTS)
