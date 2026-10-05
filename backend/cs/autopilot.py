"""The parts that keep the side hustle running without the owner (worker side):
- a daily reminder: clips waiting for review + how long the posting queue lasts;
- a weekly summary (Monday 09:00 WIB): posts and views of the last 7 days, best clip;
- stats: Instagram Reel numbers every 6 hours (insights when the token has instagram_business_manage_insights,
  otherwise likes + comments only). TikTok / YouTube numbers are typed in by the owner (their APIs need more scopes).
The owner's only daily job stays approving clips."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from . import config, db, publish, push, yt

log = logging.getLogger("cs.autopilot")

IG_METRIC_SETS = (("views", "reach", "likes", "comments", "shares", "saved"), ("reach", "likes", "comments", "shares", "saved"))


def now_wib() -> datetime:
    return datetime.now(config.TZ)


def _due(key: str, hhmm: str, period: str) -> bool:
    """True once per day (period='day') or week (period='week', Mondays) after the given WIB time."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return False
    n = now_wib()
    if (n.hour, n.minute) < (h, m) or (period == "week" and n.weekday() != 0):
        return False
    stamp = n.strftime("%G-W%V") if period == "week" else n.strftime("%Y-%m-%d")
    if db.kv_get(key) == stamp:
        return False
    db.kv_set(key, stamp)
    return True


def queue_summary() -> tuple[int, float]:
    """(clips scheduled but not posted yet, time the last one goes out)."""
    r = db.one("SELECT COUNT(DISTINCT clip_id) n, MAX(not_before) last FROM posts WHERE status='queued'")
    return r["n"] or 0, r["last"] or 0.0


async def daily_reminder():
    st = db.settings()
    if not _due("reminder_day", str(st.get("reminder_time") or "19:00"), "day"):
        return
    waiting = db.one("SELECT COUNT(*) n FROM clips WHERE status='review'")["n"]
    queued, last = queue_summary()
    per_day = max(1, len(publish.post_times()))
    if waiting == 0 and queued >= per_day * 2:
        return  # nothing to do and the queue is healthy: don't nag
    if queued:
        until = datetime.fromtimestamp(last, config.TZ).strftime("%a %H:%M")
        q = f"{queued} clip{'s' if queued > 1 else ''} scheduled (until {until})"
    else:
        q = "the posting queue is empty"
    if waiting:
        await push.send(f"🎬 {waiting} clip{'s' if waiting > 1 else ''} waiting for review", f"{q[0].upper()}{q[1:]}.",
                        url="/#review", tag="daily")
    else:
        await push.send("📭 Posting queue runs low", f"{q[0].upper()}{q[1:]}. Add a video in Sources or Browse.",
                        url="/#browse", tag="daily")


def stats_for(days: int) -> dict:
    since = time.time() - days * 86400
    posts = db.all("SELECT p.*, c.hook, c.views manual FROM posts p JOIN clips c ON c.id=p.clip_id "
                   "WHERE p.status='done' AND p.posted_at>=?", (since,))
    out = {"posts": len(posts), "by_platform": {}, "views": 0, "best": None}
    best_views = -1
    for p in posts:
        s = db.jload(p["stats"], {}) or {}
        manual = (db.jload(p["manual"], {}) or {}).get(p["platform"])
        views = int(s.get("views") or s.get("reach") or manual or 0)
        bp = out["by_platform"].setdefault(p["platform"], {"posts": 0, "views": 0})
        bp["posts"] += 1
        bp["views"] += views
        out["views"] += views
        if views > best_views:
            best_views, out["best"] = views, {"clip_id": p["clip_id"], "hook": p["hook"], "views": views,
                                              "platform": p["platform"]}
    return out


async def weekly_summary():
    if not db.settings().get("weekly_summary") or not _due("weekly_week", "09:00", "week"):
        return
    s = stats_for(7)
    if not s["posts"]:
        await push.send("📈 Last week", "No clips were posted. Approve a few to get the queue going.", url="/#review",
                        tag="weekly")
        return
    parts = [f"{v['posts']} on {k.title()}" for k, v in s["by_platform"].items()]
    best = f" Best: “{s['best']['hook'][:40]}” ({s['best']['views']:,} views)." if s["best"] and s["best"]["views"] else ""
    await push.send("📈 Last week", f"{s['posts']} posts ({', '.join(parts)}), {s['views']:,} views.{best}",
                    url="/#stats", tag="weekly")


async def youtube_visibility():
    """Google locks uploads from unaudited apps as private, sometimes hours later: check each YouTube post of the last
    3 days (videos.list with the API key: 1 unit per 50 videos; a locked video simply isn't returned)."""
    rows = db.all("SELECT * FROM posts WHERE platform='youtube' AND status='done' AND remote_id!='' AND posted_at>?",
                  (time.time() - 3 * 86400,))
    if not rows:
        return
    found = {v["id"]: v for v in await yt.videos([r["remote_id"] for r in rows])}
    for r in rows:
        st = db.jload(r["stats"], {}) or {}
        v = found.get(r["remote_id"])
        if v:
            st.update(views=v.get("views", 0), public=1)
            st.pop("locked", None)
        elif not st.get("locked"):
            st.update(locked=1, public=0)
            clip = db.one("SELECT hook FROM clips WHERE id=?", (r["clip_id"],)) or {}
            db.event(f"YouTube made clip {r['clip_id']} private (Google's API audit not passed yet)", "publish", "warn")
            await push.send("🔒 YouTube locked a Short as private", f"“{(clip.get('hook') or '')[:60]}” — share it from "
                            "the phone instead until Google's audit passes.", url=f"/#clip/{r['clip_id']}", tag=f"ytl{r['id']}")
        db.update("posts", r["id"], {"stats": db.jdump(st), "stats_at": time.time()})


async def instagram_stats():
    """Refresh numbers for Instagram posts of the last 45 days (oldest data first)."""
    tok = publish.ig_token()
    if not tok:
        return
    rows = db.all("SELECT * FROM posts WHERE platform='instagram' AND status='done' AND remote_id!='' AND "
                  "posted_at>? ORDER BY stats_at LIMIT 40", (time.time() - 45 * 86400,))
    async with httpx.AsyncClient(timeout=20) as http:
        for p in rows:
            stats, note = {}, ""
            for metrics in IG_METRIC_SETS:
                r = await http.get(f"{publish.IG_API}/{p['remote_id']}/insights",
                                   params={"metric": ",".join(metrics), "access_token": tok})
                if r.status_code == 200:
                    for item in r.json().get("data") or []:
                        vals = item.get("values") or [{}]
                        stats[item["name"]] = int((vals[0] or {}).get("value") or item.get("total_value", {}).get("value") or 0)
                    break
                note = r.text[:200]
            if not stats:  # no insights permission: basic numbers only
                r = await http.get(f"{publish.IG_API}/{p['remote_id']}",
                                   params={"fields": "like_count,comments_count", "access_token": tok})
                if r.status_code == 200:
                    d = r.json()
                    stats = {"likes": int(d.get("like_count") or 0), "comments": int(d.get("comments_count") or 0),
                             "basic": 1}
                db.kv_set("ig_insights_error", note or "no insights")
            else:
                db.kv_set("ig_insights_error", "")
            db.update("posts", p["id"], {"stats": db.jdump(stats), "stats_at": time.time()})
