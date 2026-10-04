"""Watching channels (runs in the worker): turns "new video" / "went live" signals from YouTube, Twitch and Kick
into sources. Rules (owner, 2026-10-04): the gate first; one live recording at a time; a second stream that goes
live meanwhile is clipped later from its replay; only videos newer than the channel's addition are taken."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from . import db, gate, kick, twitch, yt

log = logging.getLogger("cs.watch")


def ts_of(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def channels(platform: str) -> list[dict]:
    return db.all("SELECT * FROM channels WHERE platform=? AND enabled=1 AND permission!='blocked'", (platform,))


def recording_busy() -> bool:
    return bool(db.one("SELECT 1 FROM jobs WHERE kind='record' AND status IN ('queued','running') LIMIT 1"))


def new_source(ch: dict, *, url: str, video_id: str, title: str, kind: str, duration: float = 0,
               status: str = "queued", reason: str = "") -> int:
    now = db.now()
    return db.insert("sources", {
        "channel_id": ch["id"], "platform": ch["platform"], "url": url, "video_id": video_id, "kind": kind,
        "title": title[:300], "creator": ch["title"][:120], "creator_url": ch["url"], "duration": duration,
        "permission": ch["permission"], "proof": ch["proof"], "status": status, "reason": reason,
        "created_at": now, "updated_at": now, "added_by": "watcher"})


def refused(ch: dict, title: str, duration: float, live: bool) -> str:
    return gate.check(permission=ch["permission"], title=title, duration=duration, live=live, settings=db.settings(),
                      min_minutes=ch["min_minutes"])


def new_vod(ch: dict, *, url: str, video_id: str, title: str, duration: float, published: float):
    st = db.settings()
    if published and time.time() - published > float(st["auto_max_age_hours"]) * 3600:
        return
    if published and published < ch["created_at"] - 3600:
        return  # older than the channel's addition: no backlog
    reason = refused(ch, title, duration, live=False)
    if reason:
        new_source(ch, url=url, video_id=video_id, title=title, kind="vod", duration=duration,
                   status="rejected_by_gate", reason=reason)
        db.event(f"{ch['title']}: refused “{title[:60]}” ({reason})", "gate", "warn")
        return
    sid = new_source(ch, url=url, video_id=video_id, title=title, kind="vod", duration=duration)
    db.enqueue("process", source_id=sid, priority=10)
    db.event(f"{ch['title']}: new video “{title[:60]}” queued", "watch")


def start_live(ch: dict, *, url: str, live_id: str, title: str):
    reason = refused(ch, title, 0, live=True)
    if reason:
        new_source(ch, url=url, video_id=live_id, title=title, kind="live", status="rejected_by_gate", reason=reason)
        return
    if recording_busy():
        if ch["platform"] == "kick":
            db.event(f"{ch['title']} went live while another stream was recording; Kick has no replays to fetch",
                     "watch", "warn")
            return
        new_source(ch, url=url, video_id=live_id, title=title, kind="replay", status="waiting_replay",
                   reason="Another stream was recording; the replay will be clipped after this stream ends.")
        db.event(f"{ch['title']} went live while recording another stream: replay queued", "watch")
        return
    sid = new_source(ch, url=url, video_id=live_id, title=title, kind="live")
    db.enqueue("record", source_id=sid, priority=10)
    db.event(f"{ch['title']} is live: recording “{title[:60]}”", "watch")


# ---- YouTube --------------------------------------------------------------------------------------------
async def youtube_ids(ch: dict, ids: list[str]):
    fresh = [i for i in dict.fromkeys(ids) if not db.one("SELECT 1 FROM seen WHERE platform='youtube' AND video_id=?",
                                                         (i,))]
    if not fresh:
        return
    upcoming = db.kv_get("yt_upcoming", {}) or {}
    for v in await yt.videos(fresh):
        url = f"https://www.youtube.com/watch?v={v['id']}"
        if v["state"] == "live":
            db.mark_seen("youtube", v["id"])
            upcoming.pop(v["id"], None)
            if ch["watch_live"]:
                start_live(ch, url=url, live_id=v["id"], title=v["title"])
        elif v["state"] == "upcoming":
            if ch["watch_live"]:
                upcoming[v["id"]] = {"channel": ch["id"], "at": ts_of(v["scheduled"]) or time.time()}
        else:
            db.mark_seen("youtube", v["id"])
            upcoming.pop(v["id"], None)
            if ch["watch_uploads"]:
                new_vod(ch, url=url, video_id=v["id"], title=v["title"], duration=v["duration"],
                        published=ts_of(v["published"]))
    db.kv_set("yt_upcoming", upcoming)


async def youtube_upcoming():
    upcoming = db.kv_get("yt_upcoming", {}) or {}
    due = [vid for vid, u in upcoming.items() if u["at"] <= time.time() + 600]
    if not due:
        return
    found = await yt.videos(due)
    for vid in set(due) - {v["id"] for v in found}:
        if time.time() - upcoming[vid]["at"] > 6 * 3600:
            upcoming.pop(vid, None)  # deleted or made private
    for v in found:
        u = upcoming.get(v["id"])
        ch = db.one("SELECT * FROM channels WHERE id=?", (u["channel"],)) if u else None
        if not ch:
            upcoming.pop(v["id"], None)
            continue
        if v["state"] == "live":
            upcoming.pop(v["id"], None)
            db.mark_seen("youtube", v["id"])
            start_live(ch, url=f"https://www.youtube.com/watch?v={v['id']}", live_id=v["id"], title=v["title"])
        elif v["state"] == "none" or time.time() - u["at"] > 6 * 3600:
            upcoming.pop(v["id"], None)
    db.kv_set("yt_upcoming", upcoming)


async def youtube_poll():
    for ch in channels("youtube"):
        try:
            entries = await yt.rss(ch["ext_id"])
            await youtube_ids(ch, [e["video_id"] for e in entries if ts_of(e["published"]) > ch["created_at"] - 3600])
            db.update("channels", ch["id"], {"last_checked": db.now(), "last_error": ""})
        except Exception as e:  # noqa: BLE001 - one channel must not stop the others
            db.update("channels", ch["id"], {"last_error": str(e)[:300]})
            log.warning("youtube poll %s: %s", ch["handle"], e)


async def youtube_subscriptions():
    for ch in channels("youtube"):
        sub = db.jload(ch["sub"], {}) or {}
        if sub.get("lease_until", 0) > time.time() + 2 * 86400 and sub.get("verified"):
            continue
        if sub.get("requested", 0) > time.time() - 6 * 3600:
            continue
        ok = await yt.subscribe(ch["id"], ch["ext_id"])
        sub["requested"] = time.time()
        db.update("channels", ch["id"], {"sub": db.jdump(sub), "last_error": "" if ok else "WebSub request failed"})


# ---- Twitch ---------------------------------------------------------------------------------------------
async def twitch_poll(uploads: bool):
    chs = channels("twitch")
    if not chs or not twitch.configured():
        return
    live = await twitch.live_streams([c["ext_id"] for c in chs])
    for ch in chs:
        s = live.get(ch["ext_id"])
        db.update("channels", ch["id"], {"live_now": int(bool(s)), "last_checked": db.now(), "last_error": ""})
        if s and ch["watch_live"] and db.mark_seen("twitch", f"live:{s['id']}"):
            start_live(ch, url=ch["url"], live_id=s["id"], title=s.get("title") or ch["title"])
        if uploads and ch["watch_uploads"]:
            for v in await twitch.videos(ch["ext_id"], "upload"):
                if db.mark_seen("twitch", f"vod:{v['id']}"):
                    new_vod(ch, url=v["url"], video_id=v["id"], title=v.get("title", ""),
                            duration=twitch.vod_seconds(v.get("duration", "")), published=ts_of(v.get("created_at", "")))
    # replays of streams we couldn't record: the archived VOD with the same stream id
    for src in db.all("SELECT s.*, c.ext_id FROM sources s JOIN channels c ON c.id=s.channel_id "
                      "WHERE s.status='waiting_replay' AND s.platform='twitch'"):
        if src["ext_id"] in live:
            continue
        for v in await twitch.videos(src["ext_id"], "archive", 5):
            if str(v.get("stream_id")) == src["video_id"]:
                db.update("sources", src["id"], {"url": v["url"], "status": "queued", "reason": "",
                                                 "duration": twitch.vod_seconds(v.get("duration", "")),
                                                 "updated_at": db.now()})
                db.enqueue("process", source_id=src["id"], priority=10)
                break
        else:
            if time.time() - src["created_at"] > 24 * 3600:
                db.update("sources", src["id"], {"status": "failed", "reason": "No replay (VOD) was published."})


async def twitch_subscriptions():
    if not twitch.configured():
        return
    for ch in channels("twitch"):
        sub = db.jload(ch["sub"], {}) or {}
        if sub.get("eventsub") and sub.get("checked", 0) > time.time() - 86400:
            continue
        try:
            sub["eventsub"] = await twitch.subscribe(ch["ext_id"]) or sub.get("eventsub") or ["ok"]
            sub["checked"] = time.time()
            db.update("channels", ch["id"], {"sub": db.jdump(sub)})
        except twitch.TwitchError as e:
            db.update("channels", ch["id"], {"last_error": str(e)[:300]})


# ---- Kick -----------------------------------------------------------------------------------------------
async def kick_poll():
    chs = channels("kick")
    if not chs or not kick.configured():
        return
    by_slug = {c["handle"]: c for c in chs}
    for c in await kick.channels(list(by_slug)):
        ch = by_slug.get(c.get("slug", ""))
        if not ch:
            continue
        stream = c.get("stream") or {}
        is_live = bool(stream.get("is_live"))
        db.update("channels", ch["id"], {"live_now": int(is_live), "last_checked": db.now(), "last_error": ""})
        live_id = f"{c.get('broadcaster_user_id')}:{stream.get('start_time', '')}"
        if is_live and ch["watch_live"] and db.mark_seen("kick", live_id):
            start_live(ch, url=ch["url"], live_id=live_id, title=c.get("stream_title") or ch["title"])


# ---- YouTube replays + webhook inbox -------------------------------------------------------------------
async def youtube_replays():
    waiting = db.all("SELECT * FROM sources WHERE status='waiting_replay' AND platform='youtube'")
    if not waiting:
        return
    info = {v["id"]: v for v in await yt.videos([s["video_id"] for s in waiting])}
    for src in waiting:
        v = info.get(src["video_id"])
        if v and v["state"] == "none":
            db.update("sources", src["id"], {"status": "queued", "reason": "", "duration": v["duration"],
                                             "updated_at": db.now()})
            db.enqueue("process", source_id=src["id"], priority=10)
        elif not v and time.time() - src["created_at"] > 24 * 3600:
            db.update("sources", src["id"], {"status": "failed", "reason": "The replay isn't available."})


async def drain_inbox():
    for row in db.all("SELECT * FROM inbox ORDER BY id LIMIT 50"):
        db.execute("DELETE FROM inbox WHERE id=?", (row["id"],))
        p = db.jload(row["payload"], {}) or {}
        try:
            if row["kind"] == "youtube":
                ch = db.one("SELECT * FROM channels WHERE id=? AND enabled=1 AND permission!='blocked'", (p.get("channel"),))
                if ch:
                    await youtube_ids(ch, p.get("ids") or [])
            elif row["kind"] == "twitch_online":
                ch = db.one("SELECT * FROM channels WHERE platform='twitch' AND ext_id=? AND enabled=1 AND "
                            "permission!='blocked'", (p.get("user_id"),))
                if ch and ch["watch_live"] and db.mark_seen("twitch", f"live:{p.get('stream_id')}"):
                    title = ch["title"]
                    try:
                        s = (await twitch.live_streams([ch["ext_id"]])).get(ch["ext_id"])
                        title = (s or {}).get("title") or title
                    except twitch.TwitchError:
                        pass
                    start_live(ch, url=ch["url"], live_id=str(p.get("stream_id")), title=title)
        except Exception as e:  # noqa: BLE001
            log.warning("inbox %s: %s", row["kind"], e)
