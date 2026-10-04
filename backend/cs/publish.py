"""Auto-posting of approved clips (owner's rule: nothing publishes without approval — a post is only queued after the
owner approved the clip and its final render finished).

One row per (clip, platform) in `posts` makes it idempotent: a retry never posts twice. Posts go out one at a time,
spaced apart. Instagram: Reels via the Instagram API with Instagram Login (own account as Instagram Tester, no app
review). Instagram Login has no direct upload, so Instagram fetches the final from a temporary signed link."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from pathlib import Path

import httpx

from . import config, db, tiktok
from .posttext import post_caption

log = logging.getLogger("cs.publish")

PLATFORMS = ("instagram", "tiktok", "youtube")
IG_API = "https://graph.instagram.com/v24.0"
SPACING = 90  # seconds between two posts on the same platform


class PublishError(Exception):
    pass


class Later(Exception):
    """Not done yet (processing / rate limit): check again at `until`."""

    def __init__(self, until: float, why: str):
        super().__init__(why)
        self.until = until


def connected(platform: str) -> bool:
    if platform == "instagram":
        return bool(ig_token())
    if platform == "tiktok":
        return tiktok.connected()
    return False


def autopost_enabled(platform: str) -> bool:
    return bool((db.settings().get("autopost") or {}).get(platform)) and connected(platform)


def queue_for(clip_id: int):
    """Called when an approved clip's final is ready."""
    for p in PLATFORMS:
        if autopost_enabled(p):
            db.execute("INSERT OR IGNORE INTO posts(clip_id, platform, status, created_at, updated_at) "
                       "VALUES(?,?,?,?,?)", (clip_id, p, "queued", db.now(), db.now()))


def set_post(pid: int, **kw):
    kw["updated_at"] = db.now()
    db.update("posts", pid, kw)


# ---- Instagram ---------------------------------------------------------------------------------------
def ig_token() -> str:
    return (db.kv_get("ig_token", {}) or {}).get("token") or config.IG_ACCESS_TOKEN


async def ig_refresh(force: bool = False):
    """Long-lived tokens last 60 days; refresh about once a day (allowed once the token is 24 h old)."""
    t = db.kv_get("ig_token", {}) or {}
    if not force and t.get("refreshed", 0) > time.time() - 86400:
        return
    tok = ig_token()
    if not tok:
        return
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get("https://graph.instagram.com/refresh_access_token",
                           params={"grant_type": "ig_refresh_token", "access_token": tok})
    if r.status_code == 200 and r.json().get("access_token"):
        d = r.json()
        db.kv_set("ig_token", {"token": d["access_token"], "refreshed": time.time(),
                               "expires": time.time() + float(d.get("expires_in", 0))})
    else:
        db.kv_set("ig_token", {**t, "token": tok, "refreshed": time.time() - 86400 + 3 * 3600,
                               "error": r.text[:200]})
        log.warning("instagram token refresh: %s %s", r.status_code, r.text[:200])


async def ig_me() -> dict:
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{IG_API}/me", params={"fields": "user_id,username,account_type", "access_token": ig_token()})
    if r.status_code != 200:
        raise PublishError(f"Instagram: {r.text[:200]}")
    return r.json()


def _ig_error(r: httpx.Response) -> str:
    try:
        e = r.json().get("error", {})
        return f"Instagram {r.status_code}: {e.get('error_user_msg') or e.get('message') or r.text[:200]}"
    except ValueError:
        return f"Instagram {r.status_code}: {r.text[:200]}"


def signed_url(clip_id: int, ttl: int = 3600) -> str:
    """A temporary public link to a final video, for a platform to fetch (Instagram Login has no direct upload).
    Valid for `ttl` seconds and only while that clip has a post on its way (checked by the web app)."""
    exp = int(time.time()) + ttl
    return f"{config.PUBLIC_URL}/api/pub/{clip_id}/{exp}/{sign(clip_id, exp)}.mp4"


def sign(clip_id: int, exp: int) -> str:
    from .yt import secret
    return hmac.new(secret().encode(), f"pub:{clip_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def valid_link(clip_id: int, exp: int, sig: str) -> bool:
    return exp > time.time() and hmac.compare_digest(sign(clip_id, exp), sig) and bool(db.one(
        "SELECT 1 FROM posts WHERE clip_id=? AND status IN ('queued','uploading','processing')", (clip_id,)))


async def instagram(post: dict, clip: dict, caption: str):
    tok = ig_token()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as http:
        container = post["remote_id"]
        if not container:
            me = await ig_me()
            r = await http.post(f"{IG_API}/{me['user_id']}/media", data={
                "media_type": "REELS", "video_url": signed_url(clip["id"]), "caption": caption[:2200],
                "share_to_feed": "true", "access_token": tok})
            if r.status_code != 200:
                raise PublishError(_ig_error(r))
            container = r.json()["id"]
            set_post(post["id"], remote_id=container, status="processing")
            raise Later(time.time() + 15, "Instagram is fetching the video")
        r = await http.get(f"{IG_API}/{container}", params={"fields": "status_code,status", "access_token": tok})
        if r.status_code != 200:
            raise PublishError(_ig_error(r))
        st = r.json()
        if st.get("status_code") == "IN_PROGRESS":
            if time.time() - post["updated_at"] > 1800:
                set_post(post["id"], remote_id="")
                raise PublishError("Instagram took more than 30 minutes to process the video.")
            raise Later(time.time() + 15, "Instagram is processing the video")
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            set_post(post["id"], remote_id="")
            raise PublishError(f"Instagram rejected the video: {st.get('status', '')[:200]}")
        me = await ig_me()
        r = await http.post(f"{IG_API}/{me['user_id']}/media_publish", data={"creation_id": container,
                                                                             "access_token": tok})
        if r.status_code != 200:
            raise PublishError(_ig_error(r))
        media_id = r.json()["id"]
        url = ""
        r = await http.get(f"{IG_API}/{media_id}", params={"fields": "permalink", "access_token": tok})
        if r.status_code == 200:
            url = r.json().get("permalink", "")
    return media_id, url


async def tiktok_draft(post: dict, clip: dict):
    """Upload to the TikTok inbox, then wait until TikTok has delivered the draft."""
    if not post["remote_id"]:
        publish_id = await tiktok.upload_draft(Path(clip["final"]))
        set_post(post["id"], remote_id=publish_id, status="processing")
        raise Later(time.time() + 10, "TikTok is processing the video")
    st, why = await tiktok.status(post["remote_id"])
    if st in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
        return post["remote_id"], ""
    if st == "FAILED":
        set_post(post["id"], remote_id="")
        raise PublishError(f"TikTok refused the video: {why or 'unknown reason'}")
    if time.time() - post["updated_at"] > 1800:
        raise PublishError("TikTok took more than 30 minutes to process the video.")
    raise Later(time.time() + 15, "TikTok is processing the video")


# ---- worker side ---------------------------------------------------------------------------------------
async def run_one() -> bool:
    """Publish the next due post. True if something was attempted."""
    post = db.one("SELECT * FROM posts WHERE status IN ('queued','uploading','processing') AND not_before<=? "
                  "ORDER BY id LIMIT 1", (time.time(),))
    if not post:
        return False
    last = db.one("SELECT MAX(posted_at) t FROM posts WHERE platform=? AND status='done'", (post["platform"],))["t"] or 0
    if post["status"] == "queued" and time.time() - last < SPACING:
        set_post(post["id"], not_before=last + SPACING)
        return False
    clip = db.one("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
    if not clip or not clip["final"] or not Path(clip["final"]).exists():
        set_post(post["id"], status="failed", error="The final video is missing.")
        return True
    src = db.one("SELECT * FROM sources WHERE id=?", (clip["source_id"],)) or {}
    caption = post_caption(clip, src, post["platform"])
    try:
        if post["platform"] == "instagram":
            remote, url = await instagram(post, clip, caption)
        elif post["platform"] == "tiktok":
            remote, url = await tiktok_draft(post, clip)
        else:
            raise PublishError(f"{post['platform']} posting isn't set up yet")
    except Later as e:
        db.update("posts", post["id"], {"not_before": e.until})  # keeps updated_at = last real change
        return True
    except (PublishError, tiktok.TikTokError, httpx.HTTPError, OSError) as e:
        attempts = post["attempts"] + 1
        if attempts < 3 and not isinstance(e, PublishError):
            set_post(post["id"], attempts=attempts, not_before=time.time() + 300 * attempts, error=str(e)[:300])
        else:
            set_post(post["id"], status="failed", attempts=attempts, error=str(e)[:500])
            db.event(f"{post['platform'].title()} post failed for clip {clip['id']}: {str(e)[:200]}", "publish", "error")
            from . import push
            await push.send(f"⚠️ {post['platform'].title()} post failed", str(e)[:120], url=f"/#clip/{clip['id']}")
        return True
    set_post(post["id"], status="done", remote_id=remote, url=url, posted_at=time.time(), error="")
    if post["platform"] == "tiktok":  # a draft in the inbox: the owner posts it (and marks it posted) in the app
        db.event(f"Clip {clip['id']} sent to the TikTok inbox as a draft", "publish")
        from . import push
        await push.send("📥 TikTok draft ready", (clip["hook"] or "Your clip")[:90] + " — open TikTok to post it",
                        url=f"/#clip/{clip['id']}", tag=f"tt{clip['id']}")
        return True
    posted = db.jload(clip["posted"], {}) or {}
    posted[post["platform"]] = url or "posted"
    db.update("clips", clip["id"], {"posted": db.jdump(posted), "status": "posted", "updated_at": db.now()})
    db.event(f"Posted clip {clip['id']} to {post['platform'].title()} {url}", "publish")
    return True


async def loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await ig_refresh()
            busy = await run_one()
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("publish loop")
            busy = False
        try:
            await asyncio.wait_for(stop.wait(), timeout=3 if busy else 10)
        except asyncio.TimeoutError:
            pass
