"""Posting schedule, reminders, stats, DB migration."""

import asyncio
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    from cs import config, db, push
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "tok")
    sent = []

    async def fake_push(title, body, **k):
        sent.append((title, body))
        return 1
    monkeypatch.setattr(push, "send", fake_push)
    db.reset_for_tests()
    yield sent
    db.reset_for_tests()


def clip(final=True):
    from cs import db
    now = db.now()
    sid = db.insert("sources", {"title": "T", "creator": "Raditya Dika", "created_at": now, "updated_at": now})
    return db.insert("clips", {"source_id": sid, "start": 0, "end": 20, "status": "ready", "final": "/x" if final else "",
                               "hook": "Hook", "created_at": now, "updated_at": now})


def test_schedule_fills_slots_in_order():
    from cs import config, db, publish
    db.kv_set("settings", {"post_times": "12:00, 18:00", "autopost": {"instagram": True, "tiktok": False}})
    a, b, c = clip(), clip(), clip()
    for x in (a, b, c):
        publish.queue_for(x)
    slots = [r["not_before"] for r in db.all("SELECT not_before FROM posts ORDER BY clip_id")]
    assert len(set(slots)) == 3 and slots == sorted(slots) and slots[0] > time.time()
    hours = [datetime.fromtimestamp(t, config.TZ).hour for t in slots]
    assert set(hours) <= {12, 18}
    # schedule off: right away
    db.kv_set("settings", {"schedule_on": False, "autopost": {"instagram": True}})
    d = clip()
    publish.queue_for(d)
    assert db.one("SELECT not_before, scheduled FROM posts WHERE clip_id=?", (d,)) == {"not_before": 0, "scheduled": 0}


def test_post_now_moves_a_scheduled_post():
    from fastapi.testclient import TestClient

    from cs import db, main, publish
    cid = clip()
    publish.queue_for(cid)
    assert db.one("SELECT not_before FROM posts WHERE platform='instagram'")["not_before"] > time.time()
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        r = c.post(f"/api/clips/{cid}/publish", json={"platform": "instagram"})
        assert r.status_code == 200
        assert db.one("SELECT not_before FROM posts WHERE platform='instagram'")["not_before"] == 0
        assert c.put("/api/settings", json={"post_times": "9:00, 25:00"}).status_code == 400
        assert c.put("/api/settings", json={"post_times": "21:00; 9:00"}).json()["settings"]["post_times"] == "09:00, 21:00"
    main.app.dependency_overrides.clear()


def test_daily_reminder_once_a_day(state, monkeypatch):
    from cs import autopilot, db
    monkeypatch.setattr(autopilot, "now_wib", lambda: datetime(2026, 10, 5, 19, 30, tzinfo=autopilot.config.TZ))
    c = clip()
    db.update("clips", c, {"status": "review"})
    asyncio.run(autopilot.daily_reminder())
    asyncio.run(autopilot.daily_reminder())
    assert len(state) == 1 and "1 clip waiting" in state[0][0]
    monkeypatch.setattr(autopilot, "now_wib", lambda: datetime(2026, 10, 6, 18, 0, tzinfo=autopilot.config.TZ))
    asyncio.run(autopilot.daily_reminder())
    assert len(state) == 1  # before 19:00 the next day


def test_stats_and_weekly(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import autopilot, db, main
    a, b = clip(), clip()
    now = time.time()
    db.execute("INSERT INTO posts(clip_id,platform,status,posted_at,stats,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (a, "instagram", "done", now - 3600, '{"views": 1200, "likes": 40}', now, now))
    db.execute("INSERT INTO posts(clip_id,platform,status,posted_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
               (b, "tiktok", "done", now - 7200, now, now))
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        assert c.put(f"/api/clips/{b}/views", json={"platform": "tiktok", "views": 3000}).status_code == 200
        r = c.get("/api/stats?days=7").json()
    main.app.dependency_overrides.clear()
    assert r["views"] == 4200 and r["clips"][0]["clip_id"] == b and r["totals"]["instagram"]["views"] == 1200
    assert r["creators"][0] == {"creator": "Raditya Dika", "posts": 2, "views": 4200}
    monkeypatch.setattr(autopilot, "now_wib", lambda: datetime(2026, 10, 5, 9, 5, tzinfo=autopilot.config.TZ))  # Monday
    asyncio.run(autopilot.weekly_summary())
    asyncio.run(autopilot.weekly_summary())
    assert len(state) == 1 and "4,200 views" in state[0][1]


def test_migration_adds_columns(tmp_path):
    from cs import config, db
    old = sqlite3.connect(config.DB_PATH)
    old.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL, platform TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'queued', remote_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
                "error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0, "
                "posted_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(clip_id, platform))")
    old.commit()
    old.close()
    db.reset_for_tests()
    cols = {r["name"] for r in db.all("PRAGMA table_info(posts)")}
    assert {"stats", "stats_at", "scheduled"} <= cols


def test_new_env_token_replaces_refreshed_copy(monkeypatch):
    from cs import config, db, publish
    db.kv_set("ig_token", {"token": "refreshed-old"})  # stored before seeds existed
    assert publish.ig_token() == "tok"                  # env token takes over once
    db.kv_set("ig_token", {**db.kv_get("ig_token"), "token": "refreshed-new"})
    assert publish.ig_token() == "refreshed-new"        # later refreshes are kept
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "regenerated")
    assert publish.ig_token() == "regenerated"


def test_youtube_visibility_check(state, monkeypatch):
    from cs import autopilot, db, yt
    a, b = clip(), clip()
    now = time.time()
    for cid, vid in ((a, "pub1"), (b, "lock1")):
        db.execute("INSERT INTO posts(clip_id,platform,status,remote_id,posted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (cid, "youtube", "done", vid, now - 60, now, now))

    async def fake_videos(ids):
        return [{"id": "pub1", "views": 42}]
    monkeypatch.setattr(yt, "videos", fake_videos)
    asyncio.run(autopilot.youtube_visibility())
    asyncio.run(autopilot.youtube_visibility())
    st = {r["remote_id"]: db.jload(r["stats"]) for r in db.all("SELECT remote_id, stats FROM posts")}
    assert st["pub1"]["public"] == 1 and st["pub1"]["views"] == 42 and st["lock1"]["locked"] == 1
    assert len([x for x in state if "locked" in x[0]]) == 1  # one push, not one per check


def test_sync_all_keeps_going_when_one_platform_fails(monkeypatch):
    from cs import autopilot, db
    ran = []

    async def boom():
        ran.append("ig")
        raise RuntimeError("instagram down")

    async def ok_fb():
        ran.append("fb")

    async def ok_yt():
        ran.append("yt")
    monkeypatch.setattr(autopilot, "instagram_stats", boom)
    monkeypatch.setattr(autopilot, "facebook_stats", ok_fb)
    monkeypatch.setattr(autopilot, "youtube_visibility", ok_yt)
    asyncio.run(autopilot.sync_all())
    assert ran == ["ig", "fb", "yt"] and db.kv_get("stats_synced") > 0


def test_pauses_and_bulk(monkeypatch):
    from fastapi.testclient import TestClient

    from cs import db, main, publish, worker
    now = db.now()
    s1 = db.insert("sources", {"title": "A", "created_at": now, "updated_at": now})
    s2 = db.insert("sources", {"title": "B", "created_at": now, "updated_at": now})
    db.enqueue("process", source_id=s1, priority=10)
    c2 = db.insert("clips", {"source_id": s2, "start": 0, "end": 20, "status": "approved", "created_at": now, "updated_at": now})
    db.enqueue("final", clip_id=c2, priority=30)
    assert worker.next_job(("final", "process"))["kind"] == "final"
    db.kv_set("settings", {"pause_posting": True})                 # finals & posting paused
    assert worker.next_job(("final", "process"))["kind"] == "process"
    db.kv_set("settings", {"pause_posting": False})
    db.update("sources", s2, {"paused": 1})                         # this video paused
    assert worker.next_job(("final", "process"))["source_id"] == s1
    db.update("sources", s1, {"paused": 1})
    assert worker.next_job(("final", "process")) is None
    # posting: paused video's queued post waits, an uploading one finishes
    from cs import config
    final = config.STATE_DIR / "final.mp4"
    final.write_bytes(b"v")
    c3 = db.insert("clips", {"source_id": s2, "start": 0, "end": 20, "status": "ready", "final": str(final), "created_at": now, "updated_at": now})
    db.execute("INSERT INTO posts(clip_id,platform,status,created_at,updated_at) VALUES(?,?,?,?,?)", (c3, "instagram", "queued", now, now))
    assert not asyncio.run(publish.run_one())
    db.execute("UPDATE posts SET status='uploading'")
    called = []

    async def fake_ig(post, clip, caption):
        called.append(1)
        return "m", "https://instagram.com/x"
    monkeypatch.setattr(publish, "instagram", fake_ig)
    assert asyncio.run(publish.run_one()) and called

    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        q = c.put("/api/queue/pause", json={"what": "processing", "paused": True}).json()
        assert q["pause_processing"] is True and s1 in q["paused_sources"]
        r1 = db.insert("clips", {"source_id": s1, "start": 0, "end": 20, "status": "review", "created_at": now, "updated_at": now})
        r2 = db.insert("clips", {"source_id": s1, "start": 30, "end": 50, "status": "review", "created_at": now, "updated_at": now})
        db.update("sources", s1, {"file": "/somewhere.mkv"})
        res = c.post("/api/clips/bulk", json={"ids": [r1, r2, 99999], "action": "reject", "reason": "bulk"}).json()
        assert res["done"] == [r1, r2] and res["skipped"][0]["id"] == 99999
        assert db.one("SELECT status FROM clips WHERE id=?", (r1,))["status"] == "rejected"
        assert c.put(f"/api/sources/{s1}/pause", json={"paused": False}).json()["paused"] is False
    main.app.dependency_overrides.clear()


def test_queue_lists_items_with_plain_reasons():
    from fastapi.testclient import TestClient

    from cs import db, main
    now = db.now()
    sid = db.insert("sources", {"title": "TITIK KUMPUL", "creator": "X", "status": "transcribing", "created_at": now, "updated_at": now})
    db.insert("jobs", {"kind": "process", "source_id": sid, "not_before": now + 3600, "created_at": now,
                       "wait_reason": "Groq daily audio budget used (26662 s today)"})
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        q = c.get("/api/queue").json()
    main.app.dependency_overrides.clear()
    it = q["making"][0]
    assert it["title"] == "TITIK KUMPUL" and it["waiting"] and "tomorrow's free transcription" in it["state"]
