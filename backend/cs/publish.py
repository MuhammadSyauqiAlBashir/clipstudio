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
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import config, db, facebook, tiktok, youtube
from .posttext import post_caption

log = logging.getLogger("cs.publish")

PLATFORMS = ("instagram", "facebook", "tiktok", "youtube")
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
    if platform == "youtube":
        return youtube.connected()
    if platform == "facebook":
        return facebook.connected()
    return False


def autopost_enabled(platform: str) -> bool:
    auto = {**db.DEFAULT_SETTINGS["autopost"], **(db.settings().get("autopost") or {})}
    return bool(auto.get(platform)) and connected(platform)


def post_times() -> list[tuple[int, int]]:
    out = []
    for part in str(db.settings().get("post_times") or "").replace(";", ",").split(","):
        try:
            h, m = (int(x) for x in part.strip().split(":"))
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
        except ValueError:
            continue
    return sorted(set(out))


def next_slot(platform: str, now: float | None = None) -> float:
    """The earliest posting time (WIB) from the schedule that no queued post of this platform has taken yet."""
    now = now or time.time()
    times = post_times()
    if not db.settings().get("schedule_on") or not times:
        return 0.0
    taken = {round(r["not_before"]) for r in db.all(
        "SELECT not_before FROM posts WHERE platform=? AND status='queued' AND scheduled=1", (platform,))}
    day = datetime.fromtimestamp(now, config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(60):
        for h, m in times:
            slot = (day + timedelta(days=d)).replace(hour=h, minute=m).timestamp()
            if slot >= now + 60 and round(slot) not in taken:
                return slot
    return 0.0


def queue_for(clip_id: int):
    """Called when an approved clip's final is ready: one post per platform, in the next free schedule slot."""
    for p in PLATFORMS:
        if p == "tiktok" and tiktok.direct_mode():
            continue  # Direct Post needs the owner's choices for every post: "Post to TikTok" on the clip
        if autopost_enabled(p):
            slot = next_slot(p)
            db.execute("INSERT OR IGNORE INTO posts(clip_id, platform, status, not_before, scheduled, created_at, "
                       "updated_at) VALUES(?,?,?,?,?,?,?)", (clip_id, p, "queued", slot, int(slot > 0), db.now(),
                                                             db.now()))


def set_post(pid: int, **kw):
    kw["updated_at"] = db.now()
    db.update("posts", pid, kw)


# ---- Instagram ---------------------------------------------------------------------------------------
def ig_token() -> str:
    """The refreshed token in the DB, unless the owner saved a new one in the env file since (e.g. regenerated
    with more permissions): then that one wins."""
    t = db.kv_get("ig_token", {}) or {}
    seed = hashlib.sha256(config.IG_ACCESS_TOKEN.encode()).hexdigest()[:16] if config.IG_ACCESS_TOKEN else ""
    if seed and t.get("seed") != seed:
        db.kv_set("ig_token", {"token": config.IG_ACCESS_TOKEN, "seed": seed, "refreshed": 0})
        return config.IG_ACCESS_TOKEN
    return t.get("token") or config.IG_ACCESS_TOKEN


async def ig_refresh(force: bool = False):
    """Long-lived tokens last 60 days; refresh about once a day (allowed once the token is 24 h old)."""
    tok = ig_token()  # first: it may replace the stored token with a newly saved one
    t = db.kv_get("ig_token", {}) or {}
    if not tok or (not force and t.get("refreshed", 0) > time.time() - 86400):
        return
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get("https://graph.instagram.com/refresh_access_token",
                           params={"grant_type": "ig_refresh_token", "access_token": tok})
    if r.status_code == 200 and r.json().get("access_token"):
        d = r.json()
        db.kv_set("ig_token", {**t, "token": d["access_token"], "refreshed": time.time(),
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
            r2 = await http.get(f"{IG_API}/{container}", params={"fields": "error_message", "access_token": tok})
            detail = (r2.json().get("error_message") if r2.status_code == 200 else "") or st.get("status", "")
            raise PublishError(f"Instagram rejected the video: {detail[:250]}")
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


async def facebook_reel(post: dict, clip: dict, caption: str):
    """Upload + publish a Reel on the Page, then wait until Facebook has finished it."""
    if not post["remote_id"]:
        vid = await facebook.start_and_upload(Path(clip["final"]))
        set_post(post["id"], remote_id=vid, status="uploading")
        await facebook.finish(vid, caption)
        set_post(post["id"], status="processing")
        raise Later(time.time() + 15, "Facebook is processing the reel")
    st, pub, link = await facebook.status(post["remote_id"])
    if st == "error":
        raise PublishError("Facebook couldn't process the video.")
    if st == "ready" and pub in ("complete", ""):
        return post["remote_id"], link or f"https://www.facebook.com/reel/{post['remote_id']}"
    if time.time() - post["updated_at"] > 1800:
        raise PublishError("Facebook took more than 30 minutes to publish the reel.")
    raise Later(time.time() + 15, "Facebook is processing the reel")


async def youtube_short(post: dict, clip: dict, caption: str, src: dict):
    if post["remote_id"]:  # uploaded already (a retry after a crash): never upload twice
        return post["remote_id"], f"https://www.youtube.com/shorts/{post['remote_id']}"
    tags = [w for w in caption.split() if w.startswith("#")]
    lang = {"indonesian": "id", "english": "en"}.get((src.get("language") or "").lower(), "")
    v = await youtube.upload(Path(clip["final"]), clip["hook"] or src.get("title", ""), caption, tags, lang)
    set_post(post["id"], remote_id=v["id"])
    if v["privacy"] == "private":
        db.event(f"YouTube kept clip {clip['id']} private (Google's audit not passed yet): {v['url']}", "publish", "warn")
    return v["id"], v["url"]


async def tiktok_direct(post: dict, clip: dict):
    """Post straight to the profile with the owner's choices (posts.options), then wait until TikTok has published it."""
    opts = db.jload(post["options"], {}) or {}
    if not opts.get("privacy_level"):
        raise PublishError("TikTok Direct Post needs your choices — open the clip and tap Post to TikTok.")
    if not post["remote_id"]:
        publish_id = await tiktok.direct_post(Path(clip["final"]), opts)
        set_post(post["id"], remote_id=publish_id, status="processing")
        raise Later(time.time() + 10, "TikTok is processing the video")
    d = await tiktok.status_full(post["remote_id"])
    st = d.get("status", "")
    if st == "PUBLISH_COMPLETE":
        ids = [str(i) for i in (d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or [])]
        user = opts.get("username", "")
        url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else ""
        return post["remote_id"], url
    if st == "FAILED":
        raise PublishError(f"TikTok refused the post: {d.get('fail_reason') or 'unknown reason'}")
    if time.time() - post["updated_at"] > 1800:
        raise PublishError("TikTok took more than 30 minutes to process the video.")
    raise Later(time.time() + 15, "TikTok is processing the video")


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


# ---- failure reasons in plain words ------------------------------------------------------------------
NAMES = {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "youtube": "YouTube"}


def explain(platform: str, error: str) -> str:
    """A platform's error → a short named reason + what to do (the raw error stays in posts.error)."""
    p, e = NAMES.get(platform, platform.title()), (error or "").lower()
    if "unaudited_client_can_only_post_to_private_accounts" in e:
        return ("Until TikTok approves Direct Post, your TikTok account must be Private: TikTok → Profile → ☰ → "
                "Settings and privacy → Privacy → Private account ON, then post again with Only me.")
    if "unaudited_client" in e:
        return "TikTok only allows \"Only me\" posts until it approves Direct Post — choose Only me, or wait for the review."
    if "privacy_level_option_mismatch" in e:
        return "TikTok didn't accept that \"Who can see this\" choice for this account — pick another, then tap Retry."
    if "spam_risk_too_many_posts" in e or "reached_active_user_cap" in e:
        return "TikTok's daily posting limit for this account is reached — try again tomorrow."
    if "spam_risk_user_banned_from_posting" in e:
        return "TikTok has blocked posting on this account for now — check the TikTok app for a notice."
    if "needs your choices" in e:
        return "TikTok Direct Post needs your choices — open the clip and tap Post to TikTok."
    if "spam_risk_too_many_pending_share" in e:
        return "TikTok inbox full — too many unposted drafts. Post or delete them in TikTok, then tap Retry."
    if "limit how often" in e or "spam_risk" in e or "rate limit" in e or "too many" in e:
        return f"{p} posting limit (anti-spam) — wait a few hours, up to a day, then tap Retry."
    if "api access blocked" in e or "cannot call api for app" in e:
        return "Meta blocked the app — confirm the developer account at developers.facebook.com, then tap Retry."
    if "access token" in e or "session has expired" in e or "oauth" in e or "unauthorized" in e or "not connected" in e:
        return f"{p} login expired — reconnect it in More → Auto-posting, then tap Retry."
    if "uploads left" in e or "quota" in e:
        return f"{p} daily upload limit reached — tap Retry after it resets (YouTube: 14:00 WIB)."
    if "final video is missing" in e:
        return "The full-quality video file is missing — approve the clip again to remake it."
    if "download failed" in e or "fwdproxy" in e or "media could not be fetched" in e:
        return f"{p} couldn't fetch the video from our server — tap Retry."
    if "more than 30 minutes" in e:
        return f"{p} took too long to process the video — tap Retry."
    if "refused the video" in e or "couldn't process" in e or "unsupported" in e:
        return f"{p} rejected the video file — tap Retry; if it fails again, tell Claude."
    if "timed out" in e or "timeout" in e or "connect" in e or "network" in e:
        return f"Couldn't reach {p} (network problem) — tap Retry."
    return f"{p} refused the post — tap Retry; if it fails again, tell Claude (details below)."


# ---- worker side ---------------------------------------------------------------------------------------
async def run_one() -> bool:
    """Publish the next due post. True if something was attempted."""
    if db.settings().get("pause_posting"):  # posts already uploading finish (status 'uploading'/'processing')
        post = db.one("SELECT * FROM posts WHERE status IN ('uploading','processing') AND not_before<=? ORDER BY id "
                      "LIMIT 1", (time.time(),))
    else:
        post = db.one("SELECT p.* FROM posts p JOIN clips c ON c.id=p.clip_id JOIN sources s ON s.id=c.source_id "
                      "WHERE (p.status IN ('uploading','processing') OR (p.status='queued' AND s.paused=0 AND p.paused=0)) "
                      "AND p.not_before<=? ORDER BY p.id LIMIT 1", (time.time(),))
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
            direct = (db.jload(post["options"], {}) or {}).get("mode") == "direct"
            remote, url = await (tiktok_direct(post, clip) if direct else tiktok_draft(post, clip))
        elif post["platform"] == "youtube":
            remote, url = await youtube_short(post, clip, caption, src)
        elif post["platform"] == "facebook":
            remote, url = await facebook_reel(post, clip, caption)
        else:
            raise PublishError(f"{post['platform']} posting isn't set up yet")
    except Later as e:
        db.update("posts", post["id"], {"not_before": e.until})  # keeps updated_at = last real change
        return True
    except (PublishError, tiktok.TikTokError, youtube.YouTubeError, facebook.FacebookError, httpx.HTTPError, OSError) as e:
        # No automatic retries (owner, 2026-10-06): retrying into a platform's spam limit makes it worse. The post
        # stops as 'failed' with a named reason; the owner taps Retry when it makes sense.
        raw = str(e) or e.__class__.__name__
        why = explain(post["platform"], raw)
        set_post(post["id"], status="failed", attempts=post["attempts"] + 1, error=raw[:500])
        db.event(f"{NAMES.get(post['platform'], post['platform'])} post failed for clip {clip['id']}: {why}",
                 "publish", "error")
        from . import push
        await push.send(f"⚠️ {NAMES.get(post['platform'], post['platform'])} post failed", why[:150], url=f"/#clip/{clip['id']}")
        return True
    set_post(post["id"], status="done", remote_id=remote, url=url, posted_at=time.time(), error="")
    if post["platform"] == "tiktok" and (db.jload(post["options"], {}) or {}).get("mode") == "direct":
        opts = db.jload(post["options"], {}) or {}
        if opts.get("privacy_level") == "SELF_ONLY" or not url:  # private post: on the profile, visible only to you
            db.event(f"Clip {clip['id']} posted on TikTok (only you can see it)", "publish")
            return True
    elif post["platform"] == "tiktok":  # a draft in the inbox: the owner posts it (and marks it posted) in the app
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
