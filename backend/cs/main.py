"""Clip Studio web/API (FastAPI on 127.0.0.1:8400, behind Caddy at clips.bashir.my.id).
People log in with the shared PocketBase `users` (read-only: password check + refresh; no PocketBase data is
written). Only CS_ALLOWED_USERS may enter. The heavy work happens in clipstudio-worker."""

from __future__ import annotations

import calendar
import logging
import re
import shutil
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import config, db, fetch, gate, kick, pipeline, push, twitch, yt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cs")

COOKIE = "cs_session"
_pb: httpx.AsyncClient | None = None


def pb() -> httpx.AsyncClient:
    global _pb
    if _pb is None:
        _pb = httpx.AsyncClient(base_url=config.PB_URL, timeout=15)
    return _pb


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    push.vapid()
    yield
    if _pb:
        await _pb.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse({"error": f"Invalid {'.'.join(str(x) for x in first.get('loc', [])[1:]) or 'input'}."},
                        status_code=400)


PUBLIC_POSTS = ("/api/websub/callback", "/api/twitch/callback")


@app.middleware("http")
async def csrf_and_cache(request: Request, call_next):
    if (request.method not in ("GET", "HEAD") and request.url.path.startswith("/api/")
            and request.url.path not in PUBLIC_POSTS and request.headers.get("x-cs") != "1"):
        return JSONResponse({"error": "Missing request header."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


class Window:
    """Simple per-key rate limit: at most `n` events per `seconds`."""

    def __init__(self, n: int, seconds: float):
        self.n, self.seconds = n, seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, what: str):
        q = self.hits[key]
        now = time.monotonic()
        while q and now - q[0] > self.seconds:
            q.popleft()
        if len(q) >= self.n:
            raise HTTPException(429, f"Too many {what}. Try again later.")
        q.append(now)


login_limit = Window(10, 600)
hook_limit = Window(120, 60)


def ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Session:
    user: dict
    token: str

    @property
    def username(self) -> str:
        return self.user.get("username", "")


_cache: OrderedDict[str, tuple[float, dict, str]] = OrderedDict()


def allowed(record: dict) -> bool:
    return (record.get("role") or "") in ("", "admin") and record.get("username", "").lower() in config.ALLOWED_USERS


async def verify(token: str) -> tuple[dict, str] | None:
    hit = _cache.get(token)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1], hit[2]
    r = await pb().post("/api/collections/users/auth-refresh", headers={"Authorization": token})
    data = r.json() if r.content else {}
    if r.status_code != 200 or "token" not in data or not allowed(data.get("record", {})):
        _cache.pop(token, None)
        return None
    for t in (token, data["token"]):
        _cache[t] = (time.monotonic(), data["record"], data["token"])
        _cache.move_to_end(t)
    while len(_cache) > 100:
        _cache.popitem(last=False)
    return data["record"], data["token"]


def set_session(response: Response, token: str):
    response.set_cookie(COOKIE, token, max_age=30 * 24 * 3600, httponly=True, secure=not config.DEV, samesite="strict",
                        path="/")


async def current(request: Request, response: Response) -> Session:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Please log in.")
    res = await verify(token)
    if not res:
        response.delete_cookie(COOKIE, path="/")
        raise HTTPException(401, "Your session has ended. Please log in again.")
    user, fresh = res
    if fresh != token:
        set_session(response, fresh)
    return Session(user, fresh)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=72)


@app.post("/api/login")
async def login(body: Credentials, request: Request, response: Response):
    if not config.DEV:
        login_limit.check(ip(request), "login attempts")
    r = await pb().post("/api/collections/users/auth-with-password",
                        json={"identity": body.username.strip().lower(), "password": body.password})
    data = r.json() if r.content else {}
    if r.status_code == 403:
        raise HTTPException(403, "Your account is waiting for approval.")
    if r.status_code != 200 or not allowed(data.get("record", {})):
        raise HTTPException(401, "Wrong username or password, or this account has no access to Clip Studio.")
    set_session(response, data["token"])
    return {"me": {"username": data["record"]["username"]}}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    _cache.pop(request.cookies.get(COOKIE, ""), None)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(s: Session = Depends(current)):
    return {"me": {"username": s.username}}


@app.get("/api/health")
async def health():
    db.one("SELECT 1 x")
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------------------
SOURCE_FIELDS = ("id, channel_id, platform, url, video_id, kind, title, creator, creator_url, duration, permission, "
                 "proof, status, step, progress, reason, language, size, files_deleted, created_at, updated_at, added_by")


def source_out(s: dict) -> dict:
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM clips WHERE source_id=? GROUP BY status",
                                                  (s["id"],))}
    job = db.one("SELECT status, wait_reason, not_before FROM jobs WHERE source_id=? AND status IN ('queued','running') "
                 "ORDER BY id DESC LIMIT 1", (s["id"],))
    return {**s, "clips": counts, "job": job}


@app.get("/api/sources")
async def sources(limit: int = 60, s: Session = Depends(current)):
    rows = db.all(f"SELECT {SOURCE_FIELDS} FROM sources ORDER BY id DESC LIMIT ?", (min(200, limit),))
    return {"sources": [source_out(r) for r in rows]}


class SourceIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    permission: str
    proof: str = Field("", max_length=500)
    creator: str = Field("", max_length=120)


@app.post("/api/sources")
async def add_source(body: SourceIn, s: Session = Depends(current)):
    url = body.url.strip()
    if not re.match(r"^https://", url):
        raise HTTPException(400, "Paste a full https:// link.")
    if body.permission not in gate.PERMISSIONS or body.permission == "blocked":
        raise HTTPException(400, "Choose the permission you have for this video (explicit, platform-default or campaign).")
    if pipeline.free_gb() < config.MIN_FREE_DISK_GB:
        raise HTTPException(507, "Not enough free disk space right now.")
    now = db.now()
    sid = db.insert("sources", {"platform": fetch.platform_of(url), "url": url, "kind": "manual",
                                "permission": body.permission, "proof": body.proof.strip(), "creator": body.creator.strip(),
                                "status": "queued", "created_at": now, "updated_at": now, "added_by": s.username})
    db.enqueue("process", source_id=sid, priority=20)
    return {"source": source_out(db.one(f"SELECT {SOURCE_FIELDS} FROM sources WHERE id=?", (sid,)))}


@app.put("/api/uploads")
async def upload(request: Request, name: str, permission: str, title: str = "", creator: str = "", proof: str = "",
                 s: Session = Depends(current)):
    """Campaign footage: the file is streamed straight to disk (never held in memory)."""
    if permission not in gate.PERMISSIONS or permission == "blocked":
        raise HTTPException(400, "Choose the permission you have for this file.")
    ext = Path(name).suffix.lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
        raise HTTPException(400, "Upload an MP4, MOV, MKV or WEBM video.")
    size = int(request.headers.get("content-length") or 0)
    if pipeline.free_gb() - size / 1e9 < config.MIN_FREE_DISK_GB:
        raise HTTPException(507, "Not enough free disk space for this file.")
    now = db.now()
    sid = db.insert("sources", {"platform": "upload", "url": "", "kind": "upload", "title": (title or Path(name).stem)[:300],
                                "creator": creator.strip()[:120], "permission": permission, "proof": proof[:500],
                                "status": "uploading", "created_at": now, "updated_at": now, "added_by": s.username})
    wd = pipeline.work_dir(sid)
    wd.mkdir(parents=True, exist_ok=True)
    dst = wd / f"source{ext}"
    written = 0
    try:
        with dst.open("wb") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > 8e9:
                    raise HTTPException(413, "Files up to 8 GB.")
                f.write(chunk)
    except Exception:
        dst.unlink(missing_ok=True)
        db.update("sources", sid, {"status": "failed", "reason": "Upload interrupted.", "updated_at": db.now()})
        raise
    reason = gate.check(permission=permission, title=title or name, duration=0, live=False, settings=db.settings())
    if reason:
        dst.unlink(missing_ok=True)
        db.update("sources", sid, {"status": "rejected_by_gate", "reason": reason, "updated_at": db.now()})
        raise HTTPException(400, reason)
    db.update("sources", sid, {"file": str(dst), "size": written, "status": "queued", "updated_at": db.now()})
    db.enqueue("process", source_id=sid, priority=20)
    return {"source": source_out(db.one(f"SELECT {SOURCE_FIELDS} FROM sources WHERE id=?", (sid,)))}


def get_source(sid: int) -> dict:
    src = db.one(f"SELECT {SOURCE_FIELDS} FROM sources WHERE id=?", (sid,))
    if not src:
        raise HTTPException(404, "Source not found.")
    return src


@app.get("/api/sources/{sid}")
async def source_detail(sid: int, s: Session = Depends(current)):
    src = get_source(sid)
    clips = db.all("SELECT * FROM clips WHERE source_id=? ORDER BY CASE WHEN status='excluded' THEN 1 ELSE 0 END, "
                   "score DESC", (sid,))
    return {"source": source_out(src), "clips": [clip_out(c, src) for c in clips]}


@app.post("/api/sources/{sid}/retry")
async def retry_source(sid: int, s: Session = Depends(current)):
    src = get_source(sid)
    if src["status"] not in ("failed",):
        raise HTTPException(400, "Only failed sources can be retried.")
    if src["files_deleted"]:
        raise HTTPException(400, "This source's files were already cleaned up; add the link again.")
    if db.one("SELECT 1 FROM jobs WHERE source_id=? AND status IN ('queued','running')", (sid,)):
        raise HTTPException(409, "Already queued.")
    pipeline.set_source(sid, status="queued", reason="", step="")
    db.enqueue("process", source_id=sid, priority=20)
    return {"ok": True}


@app.delete("/api/sources/{sid}")
async def delete_source(sid: int, s: Session = Depends(current)):
    get_source(sid)
    if db.one("SELECT 1 FROM jobs WHERE source_id=? AND status='running'", (sid,)):
        raise HTTPException(409, "It's being worked on right now; try again when the step finishes.")
    db.execute("UPDATE jobs SET status='cancelled' WHERE source_id=? AND status='queued'", (sid,))
    for c in db.all("SELECT final FROM clips WHERE source_id=?", (sid,)):
        if c["final"]:
            Path(c["final"]).unlink(missing_ok=True)
    shutil.rmtree(pipeline.work_dir(sid), ignore_errors=True)
    db.execute("DELETE FROM clips WHERE source_id=?", (sid,))
    db.execute("DELETE FROM sources WHERE id=?", (sid,))
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------------------------------------
PLATFORM_TAGS = {"tiktok": "", "youtube": " #shorts", "instagram": " #reels"}


def post_caption(c: dict, src: dict, platform: str = "tiktok") -> str:
    st = db.settings()
    tags = " ".join(dict.fromkeys((c["hashtags"] + " " + st["hashtags"] + PLATFORM_TAGS.get(platform, "")).split()))
    creator = src.get("creator") or "the original creator"
    m = re.search(r"youtube\.com/@([\w.\-]+)|(?:twitch\.tv|kick\.com)/(\w+)", src.get("creator_url") or "")
    if m:
        creator = f"@{m.group(1) or m.group(2)}"
    text = st["caption_template"].format(hook=c["hook"], caption=c["caption"], creator=creator,
                                         source_url=src.get("url") or src.get("creator_url") or "", hashtags=tags)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clip_out(c: dict, src: dict | None = None) -> dict:
    src = src or db.one("SELECT * FROM sources WHERE id=?", (c["source_id"],)) or {}
    return {
        "id": c["id"], "source_id": c["source_id"], "start": c["start"], "end": c["end"],
        "duration": round(c["end"] - c["start"], 1), "score": c["score"], "llm_score": c["llm_score"],
        "loud": c["loud"], "laughter": bool(c["laughter"]), "reaction": bool(c["reaction"]), "music": c["music"],
        "reason": c["reason"], "hook": c["hook"], "caption": c["caption"], "hashtags": c["hashtags"],
        "owner_text": c["owner_text"], "layout": c["layout"], "status": c["status"], "note": c["note"],
        "reject_reason": c["reject_reason"], "has_preview": bool(c["preview"]), "has_final": bool(c["final"]),
        "final_size": c["final_size"], "posted": db.jload(c["posted"], {}), "decided_by": c["decided_by"],
        "created_at": c["created_at"], "updated_at": c["updated_at"],
        "source": {"title": src.get("title", ""), "creator": src.get("creator", ""), "url": src.get("url", ""),
                   "platform": src.get("platform", ""), "permission": src.get("permission", ""),
                   "files_deleted": bool(src.get("files_deleted"))},
        "captions": {p: post_caption(c, src, p) for p in PLATFORM_TAGS} if src else {},
    }


@app.get("/api/clips")
async def clips(status: str = "review", s: Session = Depends(current)):
    groups = {"review": ("review",), "approved": ("approved", "rendering", "ready"), "posted": ("posted",),
              "rejected": ("rejected", "expired")}
    sts = groups.get(status, (status,))
    marks = ",".join("?" for _ in sts)
    order = "score DESC" if status == "review" else "updated_at DESC"
    rows = db.all(f"SELECT * FROM clips WHERE status IN ({marks}) ORDER BY {order} LIMIT 200", sts)
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
    return {"clips": [clip_out(c) for c in rows], "counts": counts}


def get_clip(cid: int) -> dict:
    c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Clip not found.")
    return c


@app.get("/api/clips/{cid}")
async def clip_detail(cid: int, s: Session = Depends(current)):
    return {"clip": clip_out(get_clip(cid))}


class ClipEdit(BaseModel):
    hook: str | None = Field(None, max_length=120)
    caption: str | None = Field(None, max_length=600)
    hashtags: str | None = Field(None, max_length=300)
    owner_text: str | None = Field(None, max_length=160)


@app.patch("/api/clips/{cid}")
async def edit_clip(cid: int, body: ClipEdit, s: Session = Depends(current)):
    c = get_clip(cid)
    data = {k: v.strip() for k, v in body.model_dump().items() if v is not None}
    if not data:
        return {"clip": clip_out(c)}
    visual = any(k in data and data[k] != c[k] for k in ("hook", "owner_text"))
    if visual and c["status"] in ("rendering",):
        raise HTTPException(409, "The final is being made right now; edit again in a minute.")
    if visual and c["status"] == "ready":  # on-screen text changed: the final must be made again
        if c["final"]:
            Path(c["final"]).unlink(missing_ok=True)
        data.update(status="approved", final="", final_size=0)
        db.enqueue("final", clip_id=cid, priority=30)
    data["updated_at"] = db.now()
    db.update("clips", cid, data)
    return {"clip": clip_out(get_clip(cid))}


@app.post("/api/clips/{cid}/approve")
async def approve(cid: int, s: Session = Depends(current)):
    c = get_clip(cid)
    if c["status"] not in ("review", "rejected"):
        raise HTTPException(400, f"This clip is already {c['status']}.")
    src = db.one("SELECT * FROM sources WHERE id=?", (c["source_id"],))
    if not src or src["files_deleted"]:
        raise HTTPException(400, "The source video was already cleaned up, so the full-quality clip can't be made.")
    db.update("clips", cid, {"status": "approved", "decided_by": s.username, "decided_at": db.now(),
                             "reject_reason": "", "note": "", "updated_at": db.now()})
    db.enqueue("final", clip_id=cid, priority=30)
    return {"clip": clip_out(get_clip(cid))}


class RejectIn(BaseModel):
    reason: str = Field("", max_length=200)


@app.post("/api/clips/{cid}/reject")
async def reject(cid: int, body: RejectIn, s: Session = Depends(current)):
    c = get_clip(cid)
    if c["status"] in ("rendering",):
        raise HTTPException(409, "The final is being made right now.")
    db.execute("UPDATE jobs SET status='cancelled' WHERE clip_id=? AND status='queued'", (cid,))
    db.update("clips", cid, {"status": "rejected", "reject_reason": body.reason.strip(), "decided_by": s.username,
                             "decided_at": db.now(), "updated_at": db.now()})
    return {"clip": clip_out(get_clip(cid))}


class PostedIn(BaseModel):
    platform: str = Field(pattern="^(tiktok|youtube|instagram)$")
    url: str = Field("", max_length=300)


@app.post("/api/clips/{cid}/posted")
async def posted(cid: int, body: PostedIn, s: Session = Depends(current)):
    c = get_clip(cid)
    if c["status"] not in ("ready", "posted"):
        raise HTTPException(400, "Only finished clips can be marked as posted.")
    p = db.jload(c["posted"], {}) or {}
    p[body.platform] = body.url.strip() or "posted"
    db.update("clips", cid, {"posted": db.jdump(p), "status": "posted", "updated_at": db.now()})
    return {"clip": clip_out(get_clip(cid))}


def media_file(path: str) -> Path:
    if not path:
        raise HTTPException(404, "Not available.")
    p = Path(path).resolve()
    if not str(p).startswith(str(config.STATE_DIR.resolve())) or not p.exists():
        raise HTTPException(404, "Not available.")
    return p


@app.get("/api/clips/{cid}/preview.mp4")
async def preview(cid: int, s: Session = Depends(current)):
    return FileResponse(media_file(get_clip(cid)["preview"]), media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/clips/{cid}/thumb.jpg")
async def thumb(cid: int, s: Session = Depends(current)):
    return FileResponse(media_file(get_clip(cid)["thumb"]), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/clips/{cid}/final.mp4")
async def final_file(cid: int, download: int = 0, s: Session = Depends(current)):
    c = get_clip(cid)
    name = re.sub(r"[^\w\-]+", "-", (c["hook"] or f"clip-{cid}").lower()).strip("-")[:50] or f"clip-{cid}"
    return FileResponse(media_file(c["final"]), media_type="video/mp4",
                        filename=f"{name}-{cid}.mp4" if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------------------------------------
class ChannelIn(BaseModel):
    platform: str = Field(pattern="^(youtube|twitch|kick)$")
    link: str = Field(min_length=2, max_length=300)
    permission: str
    proof: str = Field("", max_length=500)
    campaign_name: str = Field("", max_length=100)
    campaign_rate: str = Field("", max_length=40)
    campaign_url: str = Field("", max_length=300)
    watch_uploads: bool = True
    watch_live: bool = True
    min_minutes: float = Field(5, ge=0, le=600)


def channel_out(c: dict) -> dict:
    sub = db.jload(c["sub"], {}) or {}
    return {**{k: c[k] for k in ("id", "platform", "ext_id", "handle", "title", "url", "permission", "proof",
                                 "watch_uploads", "watch_live", "min_minutes", "enabled", "live_now", "last_checked",
                                 "last_error", "created_at")},
            "campaign": db.jload(c["campaign"], {}), "push": bool(sub.get("verified") or sub.get("eventsub")),
            "lease_until": sub.get("lease_until", 0)}


@app.get("/api/channels")
async def list_channels(s: Session = Depends(current)):
    return {"channels": [channel_out(c) for c in db.all("SELECT * FROM channels ORDER BY platform, title")],
            "configured": {"youtube": bool(config.YT_API_KEY), "twitch": twitch.configured(), "kick": kick.configured()}}


@app.post("/api/channels")
async def add_channel(body: ChannelIn, s: Session = Depends(current)):
    if body.permission not in gate.PERMISSIONS:
        raise HTTPException(400, "Unknown permission.")
    try:
        if body.platform == "youtube":
            info = await yt.resolve(body.link)
        elif body.platform == "twitch":
            info = await twitch.resolve(body.link)
        else:
            info = await kick.resolve(body.link)
    except (yt.YTError, twitch.TwitchError, kick.KickError) as e:
        raise HTTPException(400, str(e)) from e
    if db.one("SELECT 1 FROM channels WHERE platform=? AND ext_id=?", (body.platform, info["ext_id"])):
        raise HTTPException(409, "That channel is already on the list.")
    cid = db.insert("channels", {
        "platform": body.platform, "ext_id": info["ext_id"], "handle": info["handle"], "title": info["title"][:120],
        "url": info["url"], "permission": body.permission, "proof": body.proof.strip(),
        "campaign": db.jdump({"name": body.campaign_name, "rate": body.campaign_rate, "url": body.campaign_url}),
        "watch_uploads": int(body.watch_uploads and body.platform != "kick"), "watch_live": int(body.watch_live),
        "min_minutes": body.min_minutes, "created_at": db.now()})
    db.kv_set("last_yt_subs", 0)  # let the worker subscribe right away
    db.kv_set("last_tw_subs", 0)
    return {"channel": channel_out(db.one("SELECT * FROM channels WHERE id=?", (cid,)))}


class ChannelEdit(BaseModel):
    permission: str | None = None
    proof: str | None = Field(None, max_length=500)
    watch_uploads: bool | None = None
    watch_live: bool | None = None
    enabled: bool | None = None
    min_minutes: float | None = Field(None, ge=0, le=600)


@app.patch("/api/channels/{cid}")
async def edit_channel(cid: int, body: ChannelEdit, s: Session = Depends(current)):
    c = db.one("SELECT * FROM channels WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Channel not found.")
    data = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump().items() if v is not None}
    if "permission" in data and data["permission"] not in gate.PERMISSIONS:
        raise HTTPException(400, "Unknown permission.")
    db.update("channels", cid, data)
    return {"channel": channel_out(db.one("SELECT * FROM channels WHERE id=?", (cid,)))}


@app.delete("/api/channels/{cid}")
async def delete_channel(cid: int, s: Session = Depends(current)):
    c = db.one("SELECT * FROM channels WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Channel not found.")
    try:
        if c["platform"] == "youtube":
            await yt.subscribe(c["id"], c["ext_id"], "unsubscribe")
        elif c["platform"] == "twitch" and twitch.configured():
            await twitch.unsubscribe_all(c["ext_id"])
    except Exception as e:  # noqa: BLE001 - removing it locally matters more
        log.warning("unsubscribe %s: %s", c["handle"], e)
    db.execute("DELETE FROM channels WHERE id=?", (cid,))
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------
# Public callbacks (signed; no login)
# ---------------------------------------------------------------------------------------------------------
@app.get("/api/websub/callback")
async def websub_verify(request: Request):
    p = request.query_params
    hook_limit.check(ip(request), "requests")
    ch = db.one("SELECT * FROM channels WHERE id=? AND platform='youtube'", (p.get("c", "0"),))
    mode, challenge = p.get("hub.mode", ""), p.get("hub.challenge", "")
    if not ch or p.get("hub.topic") != yt.topic(ch["ext_id"]) or not challenge:
        raise HTTPException(404, "Unknown subscription.")
    if mode == "subscribe":
        sub = db.jload(ch["sub"], {}) or {}
        lease = int(p.get("hub.lease_seconds") or yt.LEASE)
        sub.update(verified=True, lease_until=time.time() + lease)
        db.update("channels", ch["id"], {"sub": db.jdump(sub)})
    return PlainTextResponse(challenge[:200])


@app.post("/api/websub/callback")
async def websub_push(request: Request):
    hook_limit.check(ip(request), "requests")
    body = await request.body()
    if len(body) > 100_000:
        raise HTTPException(413, "Too large.")
    ch = db.one("SELECT * FROM channels WHERE id=? AND platform='youtube'", (request.query_params.get("c", "0"),))
    if not ch or not yt.verify_signature(ch["ext_id"], body, request.headers.get("x-hub-signature", "")):
        return Response(status_code=202)  # hubs expect 2xx; unsigned/unknown deliveries are dropped silently
    ids = [e["video_id"] for e in yt.parse_atom(body) if e["channel_id"] in ("", ch["ext_id"])]
    if ids:
        db.insert("inbox", {"kind": "youtube", "payload": db.jdump({"channel": ch["id"], "ids": ids}), "at": db.now()})
    return Response(status_code=204)


_tw_seen: OrderedDict[str, float] = OrderedDict()


@app.post("/api/twitch/callback")
async def twitch_callback(request: Request):
    hook_limit.check(ip(request), "requests")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if len(body) > 100_000 or not twitch.verify(headers, body):
        raise HTTPException(403, "Bad signature.")
    try:
        sent = calendar.timegm(time.strptime(headers["twitch-eventsub-message-timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
        if abs(time.time() - sent) > 600:
            raise HTTPException(403, "Too old.")
    except ValueError as e:
        raise HTTPException(403, "Bad timestamp.") from e
    mid = headers["twitch-eventsub-message-id"]
    if mid in _tw_seen:
        return Response(status_code=204)
    _tw_seen[mid] = time.time()
    while len(_tw_seen) > 500:
        _tw_seen.popitem(last=False)
    data = db.jload(body.decode(), {}) or {}
    kind = headers.get("twitch-eventsub-message-type", "")
    if kind == "webhook_callback_verification":
        return PlainTextResponse(str(data.get("challenge", ""))[:500])
    if kind == "notification":
        ev, sub = data.get("event") or {}, data.get("subscription") or {}
        if sub.get("type") == "stream.online" and ev.get("type", "live") == "live":
            db.insert("inbox", {"kind": "twitch_online", "at": db.now(), "payload": db.jdump(
                {"user_id": ev.get("broadcaster_user_id"), "stream_id": ev.get("id")})})
    elif kind == "revocation":
        db.event(f"Twitch revoked a subscription: {(data.get('subscription') or {}).get('status')}", "watch", "warn")
        db.kv_set("last_tw_subs", 0)
    return Response(status_code=204)


# ---------------------------------------------------------------------------------------------------------
# Push, settings, status
# ---------------------------------------------------------------------------------------------------------
@app.get("/api/push/key")
async def push_key(s: Session = Depends(current)):
    return {"key": push.public_key()}


class PushSub(BaseModel):
    endpoint: str = Field(max_length=1000)
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)
    ua: str = Field("", max_length=300)


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSub, s: Session = Depends(current)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "Bad endpoint.")
    db.execute("INSERT INTO push_subs(user,endpoint,p256dh,auth,ua,created_at) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(endpoint) DO UPDATE SET user=excluded.user, p256dh=excluded.p256dh, auth=excluded.auth",
               (s.username, body.endpoint, body.p256dh, body.auth, body.ua, db.now()))
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(s: Session = Depends(current)):
    sent = await push.send("🔔 Clip Studio", "Notifications are on.", url="/", tag="test", user=s.username)
    return {"sent": sent}


class SettingsIn(BaseModel):
    clips_per_hour: int | None = Field(None, ge=1, le=30)
    min_clip_seconds: int | None = Field(None, ge=5, le=60)
    max_clip_seconds: int | None = Field(None, ge=15, le=180)
    score_threshold: int | None = Field(None, ge=0, le=100)
    music_allowed: str | None = Field(None, pattern="^(none|faint)$")
    hashtags: str | None = Field(None, max_length=200)
    caption_template: str | None = Field(None, max_length=600)
    keyword_blocklist: str | None = Field(None, max_length=2000)
    max_source_hours: float | None = Field(None, ge=0.25, le=8)
    auto_max_age_hours: int | None = Field(None, ge=1, le=720)


@app.get("/api/settings")
async def get_settings(s: Session = Depends(current)):
    return {"settings": db.settings(), "defaults": db.DEFAULT_SETTINGS}


@app.put("/api/settings")
async def put_settings(body: SettingsIn, s: Session = Depends(current)):
    cur = db.kv_get("settings", {}) or {}
    cur.update({k: v for k, v in body.model_dump().items() if v is not None})
    if int(cur.get("min_clip_seconds", 15)) >= int(cur.get("max_clip_seconds", 60)):
        raise HTTPException(400, "The shortest clip must be shorter than the longest.")
    try:
        cur.get("caption_template", "{hook}").format(hook="", caption="", creator="", source_url="", hashtags="")
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(400, "The caption template may only use {hook} {caption} {creator} {source_url} "
                                 "{hashtags}.") from e
    db.kv_set("settings", cur)
    return {"settings": db.settings()}


@app.get("/api/status")
async def status(s: Session = Depends(current)):
    beat = db.kv_get("worker_heartbeat", 0) or 0
    q = {r["kind"]: r["n"] for r in db.all("SELECT kind, COUNT(*) n FROM jobs WHERE status IN ('queued','running') "
                                            "GROUP BY kind")}
    running = db.all("SELECT j.kind, j.source_id, j.clip_id, j.started_at, s.title, s.step, s.progress FROM jobs j "
                     "LEFT JOIN sources s ON s.id=j.source_id WHERE j.status='running'")
    du = shutil.disk_usage(config.STATE_DIR)
    return {
        "worker_alive": time.time() - beat < 60, "queue": q, "running": running,
        "disk_free_gb": round(du.free / 1e9, 1), "disk_min_gb": config.MIN_FREE_DISK_GB,
        "usage": {"groq_seconds": db.usage_get("groq_seconds"), "groq_limit": config.GROQ_DAILY_SECONDS,
                  "gemini_calls": db.usage_get("gemini_calls"), "gemini_failures": db.usage_get("gemini_failures"),
                  "yt_units": db.usage_get("yt_units"), "yt_limit": config.YT_DAILY_UNITS},
        "keys": {"gemini": bool(config.GEMINI_API_KEY), "groq": bool(config.GROQ_API_KEY),
                 "youtube": bool(config.YT_API_KEY), "twitch": twitch.configured(), "kick": kick.configured(),
                 "cookies": config.COOKIES_FILE.exists()},
        "events": db.all("SELECT at, level, kind, message, source_id FROM events ORDER BY id DESC LIMIT 40"),
    }
