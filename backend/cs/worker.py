"""clipstudio-worker: one processing job at a time (+ at most one live recording beside it), the channel
watchers and the cleanup. systemd caps it at 1 CPU core, ~900 MB RAM, low CPU/IO priority."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from . import autopilot, campaigns, config, db, fetch, groq, media, pipeline, publish, twitch, watch, yt
from . import kick as kick_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cs.worker")

MAX_ATTEMPTS = config.MAX_JOB_ATTEMPTS
stop = asyncio.Event()


def next_job(kinds: tuple[str, ...]) -> dict | None:
    """The next job to start, skipping what the owner paused (whole queues or single videos)."""
    st = db.settings()
    kinds = tuple(k for k in kinds if not ((k == "process" and st.get("pause_processing"))
                                           or (k == "final" and st.get("pause_posting"))))
    if not kinds:
        return None
    marks = ",".join("?" for _ in kinds)
    return db.one(f"SELECT j.* FROM jobs j LEFT JOIN clips c ON c.id=j.clip_id "
                  f"LEFT JOIN sources s ON s.id=COALESCE(j.source_id, c.source_id) "
                  f"WHERE j.status='queued' AND j.kind IN ({marks}) AND j.not_before<=? AND COALESCE(s.paused, 0)=0 "
                  "AND COALESCE(c.paused, 0)=0 "
                  "ORDER BY j.priority DESC, j.id LIMIT 1", (*kinds, time.time()))


def finish(job: dict, status: str, error: str = ""):
    db.update("jobs", job["id"], {"status": status, "error": error[:1000], "finished_at": db.now()})


async def run_job(job: dict):
    db.update("jobs", job["id"], {"status": "running", "started_at": db.now(), "attempts": job["attempts"] + 1,
                                  "wait_reason": ""})
    sid, cid = job["source_id"], job["clip_id"]
    try:
        if job["kind"] == "process":
            await pipeline.process(sid)
        elif job["kind"] == "final":
            await pipeline.final(cid)
        elif job["kind"] == "record":
            await pipeline.record(sid, stop)
        finish(job, "done")
    except pipeline.Wait as w:
        db.update("jobs", job["id"], {"status": "queued", "not_before": w.until, "wait_reason": w.reason,
                                      "attempts": job["attempts"]})
        if sid:
            pipeline.set_source(sid, step=f"waiting: {w.reason}")
        log.info("job %s waits: %s", job["id"], w.reason)
    except pipeline.Refused as e:
        finish(job, "failed", str(e))
        if sid and job["kind"] == "process":
            src = db.one("SELECT status FROM sources WHERE id=?", (sid,))
            if src and src["status"] not in ("rejected_by_gate",):
                pipeline.set_source(sid, status="failed", step="", reason=str(e)[:500])
    except asyncio.CancelledError:
        if job["kind"] != "record":  # a cut-off recording stays 'running': recover() processes what was recorded
            db.update("jobs", job["id"], {"status": "queued", "attempts": job["attempts"]})
        raise
    except (fetch.FetchError, media.MediaError, groq.QuotaWait, Exception) as e:  # noqa: BLE001
        log.exception("job %s failed", job["id"])
        msg = str(e) or e.__class__.__name__
        if job["attempts"] + 1 < MAX_ATTEMPTS and not isinstance(e, fetch.FetchError):
            db.update("jobs", job["id"], {"status": "queued", "not_before": time.time() + 600 * (job["attempts"] + 1),
                                          "wait_reason": f"retrying after an error: {msg[:200]}"})
            if sid:
                pipeline.set_source(sid, step=f"retrying after an error: {msg[:150]}")
        else:
            finish(job, "failed", msg)
            if sid and job["kind"] == "process":
                pipeline.set_source(sid, status="failed", step="", reason=msg[:500])
            db.event(f"Job {job['kind']} failed: {msg[:300]}", job["kind"], "error", sid)


async def heavy_loop():
    """Processing and final renders, strictly one at a time (finals first: priority 30)."""
    while not stop.is_set():
        job = next_job(("final", "process"))
        if job:
            await run_job(job)
        else:
            await asyncio.sleep(3)


async def record_loop():
    while not stop.is_set():
        job = next_job(("record",))
        if job:
            await run_job(job)
        else:
            await asyncio.sleep(5)


async def every(name: str, seconds: float, fn, *args):
    last = db.kv_get(f"last_{name}", 0) or 0
    if time.time() - last < seconds:
        return
    db.kv_set(f"last_{name}", time.time())
    try:
        await fn(*args)
    except (yt.YTError, twitch.TwitchError, kick_mod.KickError) as e:
        log.warning("%s: %s", name, e)
        db.event(f"{name}: {e}", "watch", "warn")
    except Exception:  # noqa: BLE001
        log.exception("%s failed", name)


async def watch_loop():
    while not stop.is_set():
        db.kv_set("worker_heartbeat", time.time())
        await every("inbox", 5, watch.drain_inbox)
        await every("yt_poll", 15 * 60, watch.youtube_poll)
        await every("yt_upcoming", 5 * 60, watch.youtube_upcoming)
        await every("yt_replays", 15 * 60, watch.youtube_replays)
        await every("yt_subs", 3600, watch.youtube_subscriptions)
        await every("tw_poll", 3 * 60, watch.twitch_poll, False)
        await every("tw_uploads", 30 * 60, watch.twitch_poll, True)
        await every("tw_subs", 3600, watch.twitch_subscriptions)
        await every("kick_poll", 3 * 60, watch.kick_poll)
        await every("cleanup", 3600, asyncio.to_thread, pipeline.cleanup)
        await every("reminder", 120, autopilot.daily_reminder)
        await every("weekly", 300, autopilot.weekly_summary)
        await every("stats", 3600, autopilot.sync_all)
        await every("campaigns", 3 * 3600, campaigns.refresh)
        await every("clippo_submit", 2 * 3600, campaigns.auto_submit)
        await every("trybuzzer_submit", 2 * 3600, campaigns.auto_submit_trybuzzer)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def recover():
    """After a restart: running jobs go back to the queue. An interrupted recording is processed as far as it got."""
    for job in db.all("SELECT * FROM jobs WHERE status='running'"):
        if job["kind"] == "record":
            src = db.one("SELECT * FROM sources WHERE id=?", (job["source_id"],))
            part = [f for f in pipeline.work_dir(job["source_id"]).glob("live.*")] if src else []
            if part and part[0].stat().st_size > 1_000_000:
                db.update("sources", src["id"], {"file": str(part[0]), "status": "queued"})
                db.enqueue("process", source_id=src["id"], priority=10)
            elif src:
                pipeline.set_source(src["id"], status="failed", reason="The recording was interrupted by a restart.")
            finish(job, "failed", "interrupted by a restart")
        else:
            db.update("jobs", job["id"], {"status": "queued"})
    db.execute("UPDATE clips SET status='approved' WHERE status='rendering'")


async def main():
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    recover()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker started")
    tasks = [asyncio.create_task(heavy_loop()), asyncio.create_task(record_loop()), asyncio.create_task(watch_loop()),
             asyncio.create_task(publish.loop(stop))]
    await stop.wait()
    await asyncio.wait(tasks, timeout=40)  # a recording gets its SIGINT and closes the file
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
