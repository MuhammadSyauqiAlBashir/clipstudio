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
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import campaigns, config, db, facebook, fetch, gate, kick, pipeline, publish, push, tiktok, twitch, youtube, yt
from .posttext import PLATFORM_TAGS, post_caption

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
SOURCE_FIELDS = ("id, channel_id, campaign_id, paused, platform, url, video_id, kind, title, creator, creator_url, duration, permission, "
                 "proof, status, step, progress, reason, language, size, files_deleted, created_at, updated_at, added_by")


def source_out(s: dict) -> dict:
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM clips WHERE source_id=? GROUP BY status",
                                                  (s["id"],))}
    job = db.one("SELECT status, wait_reason, not_before FROM jobs WHERE source_id=? AND status IN ('queued','running') "
                 "ORDER BY id DESC LIMIT 1", (s["id"],))
    return {**s, "clips": counts, "job": job}


@app.get("/api/queue")
async def queue_state(s: Session = Depends(current)):
    st = db.settings()
    jobs = {r["kind"]: r["n"] for r in db.all("SELECT kind, COUNT(*) n FROM jobs WHERE status='queued' GROUP BY kind")}
    running = db.all("SELECT j.kind, j.clip_id, j.source_id FROM jobs j WHERE j.status='running'")
    posts = db.one("SELECT COUNT(*) n FROM posts WHERE status IN ('queued','uploading','processing')")["n"]
    return {"pause_processing": bool(st.get("pause_processing")), "pause_posting": bool(st.get("pause_posting")),
            "queued": {"process": jobs.get("process", 0), "final": jobs.get("final", 0), "posts": posts},
            "running": running, "paused_sources": [r["id"] for r in db.all("SELECT id FROM sources WHERE paused=1")]}


class PauseIn(BaseModel):
    what: str = Field(pattern="^(processing|posting)$")
    paused: bool


@app.put("/api/queue/pause")
async def pause_queue(body: PauseIn, s: Session = Depends(current)):
    cur = db.kv_get("settings", {}) or {}
    cur[f"pause_{body.what}"] = body.paused
    db.kv_set("settings", cur)
    db.event(f"{'Paused' if body.paused else 'Resumed'} {body.what} ({s.username})", "queue")
    return await queue_state(s)


class SourcePauseIn(BaseModel):
    paused: bool


@app.put("/api/sources/{sid}/pause")
async def pause_source(sid: int, body: SourcePauseIn, s: Session = Depends(current)):
    get_source(sid)
    db.update("sources", sid, {"paused": int(body.paused)})
    return {"ok": True, "paused": body.paused}


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(approve|reject)$")
    reason: str = Field("", max_length=200)


@app.post("/api/clips/bulk")
async def bulk_clips(body: BulkIn, s: Session = Depends(current)):
    """Approve or reject many clips at once (same rules as one by one)."""
    done, skipped = [], []
    for cid in body.ids:
        try:
            if body.action == "approve":
                await approve(cid, s)
            else:
                await reject(cid, RejectIn(reason=body.reason), s)
            done.append(cid)
        except HTTPException as e:
            skipped.append({"id": cid, "error": str(e.detail)})
    return {"done": done, "skipped": skipped}


@app.get("/api/sources")
async def sources(limit: int = 60, s: Session = Depends(current)):
    rows = db.all(f"SELECT {SOURCE_FIELDS} FROM sources ORDER BY id DESC LIMIT ?", (min(200, limit),))
    return {"sources": [source_out(r) for r in rows]}


class SourceIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    permission: str
    proof: str = Field("", max_length=500)
    creator: str = Field("", max_length=120)
    campaign_id: int | None = None


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
    vid = yt.video_id_of(url) if fetch.platform_of(url) == "youtube" else ""
    if vid and db.one("SELECT 1 FROM sources WHERE video_id=? AND status NOT IN ('failed','rejected_by_gate')", (vid,)):
        raise HTTPException(409, "This video is already in your sources.")
    if body.campaign_id and not db.one("SELECT 1 FROM campaigns WHERE id=?", (body.campaign_id,)):
        raise HTTPException(400, "Unknown campaign.")
    sid = db.insert("sources", {"campaign_id": body.campaign_id, "platform": fetch.platform_of(url), "url": url,
                                "kind": "manual", "video_id": vid,
                                "permission": body.permission, "proof": body.proof.strip(), "creator": body.creator.strip(),
                                "status": "queued", "created_at": now, "updated_at": now, "added_by": s.username})
    db.enqueue("process", source_id=sid, priority=20)
    return {"source": source_out(db.one(f"SELECT {SOURCE_FIELDS} FROM sources WHERE id=?", (sid,)))}


@app.put("/api/uploads")
async def upload(request: Request, name: str, permission: str, title: str = "", creator: str = "", proof: str = "",
                 campaign_id: int = 0, s: Session = Depends(current)):
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
    sid = db.insert("sources", {"campaign_id": campaign_id or None, "platform": "upload", "url": "", "kind": "upload",
                                "title": (title or Path(name).stem)[:300],
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


@app.get("/api/browse/youtube")
async def browse_youtube(q: str = "", channel: str = "", page: str = "", tab: str = "videos",
                         s: Session = Depends(current)):
    """Search a YouTube channel (handle / link / video link / name) and list its uploads, page by page."""
    try:
        if not channel:
            channel, choices = await yt.find_channels(q)
            if not channel:
                return {"choices": choices}
        res = await yt.uploads_page(channel, page, tab)
    except (yt.YTError, fetch.FetchError) as e:
        raise HTTPException(400, str(e)) from e
    channel = res["channel"]["id"]
    ids = [v["id"] for v in res["videos"]]
    added = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in db.all(f"SELECT video_id, id, status FROM sources WHERE video_id IN ({marks})", ids):
            added[r["video_id"]] = {"id": r["id"], "status": r["status"]}
    watched = db.one("SELECT id, permission FROM channels WHERE platform='youtube' AND ext_id=?", (channel,))
    for v in res["videos"]:
        v["added"] = added.get(v["id"])
        v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
    ch = res["channel"]
    return {"channel": ch, "videos": res["videos"], "next": res["next"], "prev": res["prev"], "total": res["total"],
            "watched": watched}


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
                   "paused": bool(src.get("paused")),
                   "platform": src.get("platform", ""), "permission": src.get("permission", ""),
                   "files_deleted": bool(src.get("files_deleted"))},
        "captions": {p: post_caption(c, src, p) for p in PLATFORM_TAGS} if src else {},
        "posts": {r["platform"]: {"status": r["status"], "url": r["url"], "error": r["error"],
                                  "at": r["not_before"] if r["status"] == "queued" else r["posted_at"],
                                  "stats": db.jload(r["stats"], {})}
                  for r in db.all("SELECT * FROM posts WHERE clip_id=?", (c["id"],))},
    }


@app.get("/api/clips")
async def clips(status: str = "review", order: str = "", s: Session = Depends(current)):
    groups = {"review": ("review",), "approved": ("approved", "rendering", "ready"), "posted": ("posted",),
              "rejected": ("rejected", "expired")}
    sts = groups.get(status, (status,))
    marks = ",".join("?" for _ in sts)
    order = "source_id DESC, start" if order == "source" else "score DESC" if status == "review" else "updated_at DESC"
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
    platform: str = Field(pattern="^(tiktok|youtube|instagram|facebook)$")
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


class ViewsIn(BaseModel):
    platform: str = Field(pattern="^(tiktok|youtube|instagram|facebook)$")
    views: int = Field(ge=0, le=10_000_000_000)


@app.put("/api/clips/{cid}/views")
async def set_views(cid: int, body: ViewsIn, s: Session = Depends(current)):
    """Views typed in by the owner (TikTok / YouTube numbers aren't read automatically)."""
    c = get_clip(cid)
    v = db.jload(c["views"], {}) or {}
    v[body.platform] = body.views
    db.update("clips", cid, {"views": db.jdump(v), "updated_at": db.now()})
    return {"clip": clip_out(get_clip(cid))}


@app.get("/api/stats")
async def stats(days: int = 30, s: Session = Depends(current)):
    days = max(1, min(365, days))
    since = time.time() - days * 86400
    rows = db.all("SELECT p.platform, p.status, p.posted_at, p.url, p.stats, c.id clip_id, c.hook, c.views manual, "
                  "c.score, src.creator, src.title src_title FROM posts p JOIN clips c ON c.id=p.clip_id "
                  "LEFT JOIN sources src ON src.id=c.source_id WHERE p.status='done' AND p.posted_at>=? "
                  "ORDER BY p.posted_at DESC", (since,))
    clips: dict[int, dict] = {}
    creators: dict[str, dict] = {}
    days_count: dict[str, int] = {}
    for r in rows:
        st = db.jload(r["stats"], {}) or {}
        manual = (db.jload(r["manual"], {}) or {}).get(r["platform"])
        views = int(st.get("views") or st.get("reach") or manual or 0)
        c = clips.setdefault(r["clip_id"], {"clip_id": r["clip_id"], "hook": r["hook"], "creator": r["creator"] or "",
                                            "score": r["score"], "posted_at": r["posted_at"], "views": 0,
                                            "platforms": {}})
        c["platforms"][r["platform"]] = {"views": views, "likes": st.get("likes"), "comments": st.get("comments"),
                                         "shares": st.get("shares"), "url": r["url"], "manual": manual is not None,
                                         "basic": bool(st.get("basic"))}
        c["views"] += views
        c["posted_at"] = min(c["posted_at"], r["posted_at"])
        cr = creators.setdefault(r["creator"] or "?", {"creator": r["creator"] or "?", "posts": 0, "views": 0})
        cr["posts"] += 1
        cr["views"] += views
        day = datetime.fromtimestamp(r["posted_at"], config.TZ).strftime("%Y-%m-%d")
        days_count[day] = days_count.get(day, 0) + 1
    queued = db.one("SELECT COUNT(DISTINCT clip_id) n, MAX(not_before) last FROM posts WHERE status='queued'")
    totals = {p: {"posts": 0, "views": 0} for p in publish.PLATFORMS}
    for c in clips.values():
        for p, v in c["platforms"].items():
            totals[p]["posts"] += 1
            totals[p]["views"] += v["views"]
    return {"days": days, "totals": totals, "views": sum(c["views"] for c in clips.values()),
            "clips": sorted(clips.values(), key=lambda c: -c["views"]),
            "creators": sorted(creators.values(), key=lambda c: -c["views"]), "per_day": days_count,
            "queue": {"clips": queued["n"] or 0, "until": queued["last"] or 0},
            "review_waiting": db.one("SELECT COUNT(*) n FROM clips WHERE status='review'")["n"],
            "ig_insights": not (db.kv_get("ig_insights_error") or ""), "synced": db.kv_get("stats_synced", 0) or 0}


# ---------------------------------------------------------------------------------------------------------
# Campaigns (public catalogue; joining happens in the platform's own app)
# ---------------------------------------------------------------------------------------------------------
def campaign_out(r: dict) -> dict:
    srcs = db.all("SELECT id FROM sources WHERE campaign_id=?", (r["id"],))
    ids = [x["id"] for x in srcs]
    to_submit = 0
    clips_n = 0
    if ids:
        marks = ",".join("?" for _ in ids)
        for c in db.all(f"SELECT posted, submitted, status FROM clips WHERE source_id IN ({marks})", ids):
            clips_n += c["status"] not in ("candidate", "excluded")
            posted, sub = db.jload(c["posted"], {}) or {}, db.jload(c["submitted"], {}) or {}
            to_submit += sum(1 for p, u in posted.items() if str(u).startswith("http") and p not in sub)
    return {**{k: r[k] for k in ("id", "platform", "ext_id", "title", "creator", "rate", "budget_used", "clippers",
                                 "brief", "image", "url", "status", "joined", "hidden", "notes", "first_seen")},
            "platforms": db.jload(r["platforms"], []), "hashtags": db.jload(r["hashtags"], []),
            "footage": db.jload(r["footage"], []), "flags": db.jload(r["flags"], []), "info": db.jload(r["info"], {}),
            "sources": len(ids), "clips": clips_n, "to_submit": to_submit}


@app.get("/api/campaigns")
async def list_campaigns(show: str = "open", s: Session = Depends(current)):
    where = {"open": "status='open' AND hidden=0", "joined": "joined=1", "ended": "status='ended'",
             "hidden": "hidden=1", "all": "1=1"}.get(show, "status='open' AND hidden=0")
    rows = db.all(f"SELECT * FROM campaigns WHERE {where} ORDER BY joined DESC, (status='open') DESC, rate DESC, "
                  "budget_used ASC")
    from . import clippo, trybuzzer
    return {"campaigns": [campaign_out(r) for r in rows], "refreshed": db.kv_get("campaigns_refreshed", 0) or 0,
            "clippo_connected": clippo.configured(), "trybuzzer_connected": trybuzzer.configured()}


@app.post("/api/campaigns/refresh")
async def refresh_campaigns(s: Session = Depends(current)):
    try:
        new = await campaigns.refresh(notify=False)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Clippo didn't answer: {e}") from e
    return {"new": new}


def get_campaign(cid: int) -> dict:
    r = db.one("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404, "Campaign not found.")
    return r


class CampaignEdit(BaseModel):
    joined: bool | None = None
    hidden: bool | None = None
    notes: str | None = Field(None, max_length=1000)


@app.patch("/api/campaigns/{cid}")
async def edit_campaign(cid: int, body: CampaignEdit, s: Session = Depends(current)):
    get_campaign(cid)
    data = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump().items() if v is not None}
    db.update("campaigns", cid, data)
    return {"campaign": campaign_out(get_campaign(cid))}


@app.post("/api/campaigns/{cid}/join")
async def join_campaign(cid: int, s: Session = Depends(current)):
    """Join on the platform as the owner (Clippo), then mark it joined here."""
    from . import clippo
    r = get_campaign(cid)
    if r["platform"] != "clippo" or not clippo.configured():
        raise HTTPException(400, "Joining from here works for Clippo once its session is saved.")
    try:
        await clippo.join(r["ext_id"])
    except clippo.ClippoError as e:
        if "already" not in str(e).lower():
            raise HTTPException(e.status if e.status in (400, 401, 403, 409) else 502, str(e)) from e
    db.update("campaigns", cid, {"joined": 1})
    db.event(f"Joined Clippo campaign: {r['title'][:80]}", "campaign")
    return {"campaign": campaign_out(get_campaign(cid))}


@app.post("/api/campaigns/submit-now")
async def campaigns_submit_now(s: Session = Depends(current)):
    from . import clippo
    from . import trybuzzer
    n = 0
    try:
        n += await campaigns.auto_submit()
    except clippo.ClippoError as e:
        raise HTTPException(502, str(e)) from e
    try:
        n += await campaigns.auto_submit_trybuzzer()
    except trybuzzer.TryBuzzerError as e:
        raise HTTPException(502, str(e)) from e
    return {"submitted": n}


@app.post("/api/campaigns/{cid}/clip")
async def clip_campaign(cid: int, s: Session = Depends(current)):
    """Add the campaign's YouTube footage as sources (permission 'campaign', its hashtags go into the captions)."""
    r = get_campaign(cid)
    c = {"footage": db.jload(r["footage"], [])}
    added, skipped = [], []
    now = db.now()
    for url in campaigns.youtube_footage(c):
        vid = yt.video_id_of(url)
        if vid and db.one("SELECT 1 FROM sources WHERE video_id=? AND status NOT IN ('failed','rejected_by_gate')", (vid,)):
            skipped.append(url)
            continue
        sid = db.insert("sources", {"campaign_id": cid, "platform": "youtube", "url": url, "video_id": vid,
                                    "kind": "manual", "permission": "campaign",
                                    "proof": f"{r['platform'].title()} campaign: {r['url']}", "creator": r["creator"],
                                    "status": "queued", "created_at": now, "updated_at": now, "added_by": s.username})
        db.enqueue("process", source_id=sid, priority=20)
        added.append(url)
    other = [f for f in c["footage"] if f["url"] not in campaigns.youtube_footage(c)]
    return {"added": added, "skipped": skipped, "manual": other}


@app.get("/api/campaigns/{cid}/submit")
async def campaign_submit_list(cid: int, s: Session = Depends(current)):
    get_campaign(cid)
    out = []
    for c in db.all("SELECT c.id, c.hook, c.posted, c.submitted FROM clips c JOIN sources src ON src.id=c.source_id "
                    "WHERE src.campaign_id=? ORDER BY c.updated_at DESC", (cid,)):
        posted, sub = db.jload(c["posted"], {}) or {}, db.jload(c["submitted"], {}) or {}
        for p, u in posted.items():
            if str(u).startswith("http"):
                chk = db.kv_get(f"clippo_check:{c['id']}:{p}") or {}
                out.append({"clip_id": c["id"], "hook": c["hook"], "platform": p, "url": u, "submitted": sub.get(p, 0),
                            "waiting": chk.get("reason", "") if not sub.get(p) else ""})
    return {"items": out}


class SubmittedIn(BaseModel):
    items: list[dict] = Field(max_length=200)  # [{"clip_id": 1, "platform": "instagram"}]


@app.post("/api/campaigns/{cid}/submitted")
async def campaign_submitted(cid: int, body: SubmittedIn, s: Session = Depends(current)):
    get_campaign(cid)
    for it in body.items:
        c = db.one("SELECT c.id, c.submitted FROM clips c JOIN sources src ON src.id=c.source_id "
                   "WHERE c.id=? AND src.campaign_id=?", (int(it.get("clip_id", 0)), cid))
        if c and it.get("platform") in publish.PLATFORMS:
            sub = db.jload(c["submitted"], {}) or {}
            sub[it["platform"]] = time.time()
            db.update("clips", c["id"], {"submitted": db.jdump(sub)})
    return {"ok": True}


@app.post("/api/stats/sync")
async def stats_sync(s: Session = Depends(current)):
    last = db.kv_get("stats_synced", 0) or 0
    if time.time() - last < 60:
        raise HTTPException(429, "Synced less than a minute ago.")
    from . import autopilot
    await autopilot.sync_all()
    return {"ok": True}


class PublishIn(BaseModel):
    platform: str = Field(pattern="^(tiktok|youtube|instagram|facebook)$")


@app.post("/api/clips/{cid}/publish")
async def publish_now(cid: int, body: PublishIn, s: Session = Depends(current)):
    """Post (or retry) an approved, finished clip on one platform."""
    c = get_clip(cid)
    if c["status"] not in ("ready", "posted") or not c["final"]:
        raise HTTPException(400, "Only approved clips with a finished video can be posted.")
    if not publish.connected(body.platform):
        raise HTTPException(400, f"{body.platform.title()} isn't connected yet.")
    row = db.one("SELECT * FROM posts WHERE clip_id=? AND platform=?", (cid, body.platform))
    if row and row["status"] == "queued" and row["not_before"] > time.time():  # scheduled: post it now instead
        db.execute("UPDATE posts SET not_before=0, scheduled=0, updated_at=? WHERE id=?", (db.now(), row["id"]))
        return {"clip": clip_out(get_clip(cid))}
    if row and row["status"] != "failed":
        raise HTTPException(409, "Already posted or on its way." if row["status"] == "done" else "Already on its way.")
    if row:
        db.execute("UPDATE posts SET status='queued', error='', attempts=0, not_before=0, remote_id='', updated_at=? "
                   "WHERE id=?", (db.now(), row["id"]))
    else:
        db.execute("INSERT INTO posts(clip_id, platform, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                   (cid, body.platform, "queued", db.now(), db.now()))
    return {"clip": clip_out(get_clip(cid))}


@app.get("/api/accounts")
async def accounts(s: Session = Depends(current)):
    auto = db.settings().get("autopost") or {}
    ig = {"connected": publish.connected("instagram"), "username": "", "error": ""}
    if ig["connected"]:
        cached = db.kv_get("ig_me", {}) or {}
        if cached.get("at", 0) > time.time() - 3600:
            ig["username"] = cached.get("username", "")
        else:
            try:
                me_ = await publish.ig_me()
                ig["username"] = me_.get("username", "")
                db.kv_set("ig_me", {"username": ig["username"], "at": time.time()})
            except (publish.PublishError, httpx.HTTPError) as e:
                ig["error"] = str(e)[:200]
    tok = db.kv_get("ig_token", {}) or {}
    ig["expires"] = tok.get("expires", 0)
    ta = tiktok.auth()
    fb = {"connected": facebook.connected(), "username": "", "error": "",
          "note": "Reels on your Facebook Page (works without review)."}
    if fb["connected"]:
        cached = db.kv_get("fb_page", {}) or {}
        if cached.get("at", 0) > time.time() - 3600:
            fb["username"] = cached.get("name", "")
        else:
            try:
                fb["username"] = (await facebook.page_info()).get("name", "")
                db.kv_set("fb_page", {"name": fb["username"], "at": time.time()})
            except (facebook.FacebookError, httpx.HTTPError) as e:
                fb["error"] = str(e)[:200]
    out = {"instagram": ig, "facebook": fb,
           "tiktok": {"connected": tiktok.connected(), "username": ta.get("display_name", ""), "error": "",
                      "can_connect": tiktok.configured(),
                      "note": "Approved clips go to your TikTok inbox as drafts; you post them in the TikTok app."},
           "youtube": {"connected": youtube.connected(), "username": youtube.auth().get("channel", ""), "error": "",
                       "can_connect": youtube.configured(),
                       "note": f"Until Google's audit passes, uploads are locked private, so keep auto-post off and "
                               f"share from the phone. {youtube.uploads_left()} of {youtube.DAILY_UPLOADS} uploads left today."}}
    for k, v in out.items():
        v["autopost"] = bool(auto.get(k))
    return {"accounts": out}


@app.get("/api/tiktok/connect")
async def tiktok_connect(s: Session = Depends(current)):
    if not tiktok.configured():
        raise HTTPException(400, "TikTok keys aren't set up on the server.")
    return RedirectResponse(tiktok.authorize_url(), status_code=302)


@app.get("/api/tiktok/oauth")
async def tiktok_oauth(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    """TikTok sends the owner back here after the login. Protected by the single-use state (no session cookie)."""
    if error or not tiktok.check_state(state):
        msg = error_description or error or "expired or invalid login link"
        return RedirectResponse("/#more?tiktok=" + quote(f"failed: {msg}"[:120]), status_code=302)
    try:
        await tiktok.finish_login(code)
    except (tiktok.TikTokError, httpx.HTTPError) as e:
        return RedirectResponse("/#more?tiktok=" + quote(f"failed: {e}"[:120]), status_code=302)
    db.event("TikTok connected", "accounts")
    return RedirectResponse("/#more?tiktok=connected", status_code=302)


@app.post("/api/tiktok/disconnect")
async def tiktok_disconnect(s: Session = Depends(current)):
    await tiktok.disconnect()
    db.event("TikTok disconnected", "accounts")
    return {"ok": True}


@app.get("/api/youtube/connect")
async def youtube_connect(s: Session = Depends(current)):
    if not youtube.configured():
        raise HTTPException(400, "YouTube keys aren't set up on the server.")
    return RedirectResponse(youtube.authorize_url(), status_code=302)


@app.get("/api/youtube/oauth")
async def youtube_oauth(code: str = "", state: str = "", error: str = ""):
    """Google sends the owner back here after the consent screen (single-use state; no session cookie)."""
    if error or not youtube.check_state(state):
        return RedirectResponse("/#more?youtube=" + quote(f"failed: {error or 'expired or invalid login link'}"[:120]),
                                status_code=302)
    try:
        await youtube.finish_login(code)
    except (youtube.YouTubeError, httpx.HTTPError) as e:
        return RedirectResponse("/#more?youtube=" + quote(f"failed: {e}"[:120]), status_code=302)
    db.event("YouTube connected", "accounts")
    return RedirectResponse("/#more?youtube=connected", status_code=302)


@app.post("/api/youtube/disconnect")
async def youtube_disconnect(s: Session = Depends(current)):
    await youtube.disconnect()
    db.event("YouTube disconnected", "accounts")
    return {"ok": True}


class AutopostIn(BaseModel):
    platform: str = Field(pattern="^(tiktok|youtube|instagram|facebook)$")
    on: bool


@app.put("/api/accounts/autopost")
async def set_autopost(body: AutopostIn, s: Session = Depends(current)):
    cur = db.kv_get("settings", {}) or {}
    auto = {**db.DEFAULT_SETTINGS["autopost"], **(cur.get("autopost") or {})}
    auto[body.platform] = body.on
    cur["autopost"] = auto
    db.kv_set("settings", cur)
    return {"autopost": auto}


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


@app.api_route("/api/pub/{cid}/{exp}/{sig}.mp4", methods=["GET", "HEAD"])
async def public_final(cid: int, exp: int, sig: str):  # HEAD too: Meta's download proxy asks for the headers first
    """Temporary signed link for a platform to fetch an approved final (no login; see publish.signed_url)."""
    if not publish.valid_link(cid, exp, sig):
        raise HTTPException(404, "Not found.")
    return FileResponse(media_file(get_clip(cid)["final"]), media_type="video/mp4",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


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
    schedule_on: bool | None = None
    post_times: str | None = Field(None, max_length=200)
    reminder_time: str | None = Field(None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")
    weekly_summary: bool | None = None


@app.get("/api/settings")
async def get_settings(s: Session = Depends(current)):
    return {"settings": db.settings(), "defaults": db.DEFAULT_SETTINGS}


@app.put("/api/settings")
async def put_settings(body: SettingsIn, s: Session = Depends(current)):
    cur = db.kv_get("settings", {}) or {}
    cur.update({k: v for k, v in body.model_dump().items() if v is not None})
    if body.post_times is not None:
        times = [t.strip() for t in body.post_times.replace(";", ",").split(",") if t.strip()]
        if not all(re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t) for t in times):
            raise HTTPException(400, "Posting times look like 12:00, 18:00, 21:00.")
        cur["post_times"] = ", ".join(sorted(set(t.zfill(5) for t in times)))
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
                  "yt_units": db.usage_get("yt_units", db.google_day()), "yt_limit": config.YT_DAILY_UNITS,
                  "yt_uploads_left": youtube.uploads_left()},
        "keys": {"gemini": bool(config.GEMINI_API_KEY), "groq": bool(config.GROQ_API_KEY),
                 "youtube": bool(config.YT_API_KEY), "twitch": twitch.configured(), "kick": kick.configured(),
                 "cookies": config.COOKIES_FILE.exists()},
        "events": db.all("SELECT at, level, kind, message, source_id FROM events ORDER BY id DESC LIMIT 40"),
    }
