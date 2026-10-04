"""Auto-posting: idempotent queue, spacing, failures (Instagram replaced by a fake)."""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    from cs import config, db, push
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "IG_ACCESS_TOKEN", "tok")

    async def no_push(*a, **k):
        return 0
    monkeypatch.setattr(push, "send", no_push)
    db.reset_for_tests()
    yield tmp_path
    db.reset_for_tests()


def make_clip(tmp_path):
    from cs import db
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    now = db.now()
    sid = db.insert("sources", {"title": "T", "creator": "C", "url": "https://x", "created_at": now, "updated_at": now})
    return db.insert("clips", {"source_id": sid, "start": 0, "end": 20, "status": "ready", "final": str(final),
                               "hook": "Hook", "caption": "Cap", "hashtags": "#a", "created_at": now, "updated_at": now})


def test_queue_is_idempotent_and_respects_switches(state):
    from cs import db, publish
    cid = make_clip(state)
    publish.queue_for(cid)
    publish.queue_for(cid)
    rows = db.all("SELECT platform FROM posts WHERE clip_id=?", (cid,))
    assert [r["platform"] for r in rows] == ["instagram"]  # tiktok/youtube not connected, youtube off by default


def test_post_success_failure_and_spacing(state, monkeypatch):
    from cs import db, publish
    calls = []

    async def fake_ig(post, clip, caption):
        calls.append(caption)
        return "m1", "https://instagram.com/reel/x"
    monkeypatch.setattr(publish, "instagram", fake_ig)
    a, b = make_clip(state), make_clip(state)
    publish.queue_for(a)
    publish.queue_for(b)
    assert asyncio.run(publish.run_one())
    p = db.one("SELECT * FROM posts WHERE clip_id=?", (a,))
    assert p["status"] == "done" and p["url"].startswith("https://instagram.com")
    c = db.one("SELECT * FROM clips WHERE id=?", (a,))
    assert c["status"] == "posted" and "instagram" in c["posted"]
    assert "#reels" in calls[0] and "Hook" in calls[0]
    # the second post waits for the spacing
    assert not asyncio.run(publish.run_one())
    assert db.one("SELECT not_before FROM posts WHERE clip_id=?", (b,))["not_before"] > time.time()

    async def broken(post, clip, caption):
        raise publish.PublishError("Instagram 400: bad video")
    monkeypatch.setattr(publish, "instagram", broken)
    db.execute("UPDATE posts SET not_before=0 WHERE clip_id=?", (b,))
    db.execute("UPDATE posts SET posted_at=0")
    assert asyncio.run(publish.run_one())
    assert db.one("SELECT status, error FROM posts WHERE clip_id=?", (b,)) == {"status": "failed",
                                                                              "error": "Instagram 400: bad video"}
    assert len(calls) == 1


def test_publish_endpoint_retry_rules(state):
    from fastapi.testclient import TestClient

    from cs import db, main
    cid = make_clip(state)
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        assert c.post(f"/api/clips/{cid}/publish", json={"platform": "tiktok"}).status_code == 400  # not connected
        assert c.post(f"/api/clips/{cid}/publish", json={"platform": "instagram"}).status_code == 200
        assert c.post(f"/api/clips/{cid}/publish", json={"platform": "instagram"}).status_code == 409  # no double
        db.execute("UPDATE posts SET status='failed'")
        assert c.post(f"/api/clips/{cid}/publish", json={"platform": "instagram"}).status_code == 200  # retry
        r = c.put("/api/accounts/autopost", json={"platform": "instagram", "on": False})
        assert r.json()["autopost"]["instagram"] is False
    main.app.dependency_overrides.clear()


def test_signed_links(state):
    from fastapi.testclient import TestClient

    from cs import config, db, main, publish
    cid = make_clip(state)
    url = publish.signed_url(cid)
    path = url.split(config.PUBLIC_URL, 1)[1]
    with TestClient(main.app) as c:
        assert c.get(path).status_code == 404  # no post on its way: link refused
        publish.queue_for(cid)
        r = c.get(path)
        assert r.status_code == 200 and r.content == b"video"
        assert c.get(path.replace(".mp4", "").rstrip("0123456789abcdef") + "0" * 40 + ".mp4").status_code == 404
        _, _, _, _, exp, sig = path.split("/")
        old = int(exp) - 7200
        assert c.get(f"/api/pub/{cid}/{old}/{publish.sign(cid, old)}.mp4").status_code == 404  # expired
        db.execute("UPDATE posts SET status='done'")
        assert c.get(path).status_code == 404  # done: link dead


def test_tiktok_state_and_chunks(state, monkeypatch):
    from cs import config, tiktok
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "sbkey")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "sec")
    url = tiktok.authorize_url()
    assert "client_key=sbkey" in url and "video.upload" in url and "redirect_uri=https%3A%2F%2Fclips" in url
    st = url.split("state=")[1]
    assert not tiktok.check_state("wrong")          # wrong state burns the saved one
    url = tiktok.authorize_url()
    st = url.split("state=")[1]
    assert tiktok.check_state(st) and not tiktok.check_state(st)  # single use
    assert tiktok.chunk_plan(30 * 1024 * 1024) == (30 * 1024 * 1024, 1)
    assert tiktok.chunk_plan(100 * 1024 * 1024) == (tiktok.CHUNK, 10)


def test_tiktok_callback_and_draft(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import config, db, main, publish, tiktok
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "sbkey")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "sec")

    async def fake_finish(code):
        db.kv_set("tiktok_auth", {"access_token": "a", "refresh_token": "r", "expires": time.time() + 9999,
                                  "display_name": "bashclipeveryday"})
    monkeypatch.setattr(tiktok, "finish_login", fake_finish)
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.get("/api/tiktok/oauth?code=x&state=nope")
        assert "failed" in r.headers["location"] and not tiktok.connected()
        st = tiktok.authorize_url().split("state=")[1]
        r = c.get(f"/api/tiktok/oauth?code=x&state={st}")
        assert r.headers["location"] == "/#more?tiktok=connected" and tiktok.connected()

    cid = make_clip(state)
    publish.queue_for(cid)
    assert {r["platform"] for r in db.all("SELECT platform FROM posts")} == {"instagram", "tiktok"}

    async def up(path):
        return "pub1"
    seq = iter([("PROCESSING_UPLOAD", ""), ("SEND_TO_USER_INBOX", "")])

    async def stat(pid):
        return next(seq)
    monkeypatch.setattr(tiktok, "upload_draft", up)
    monkeypatch.setattr(tiktok, "status", stat)
    db.execute("DELETE FROM posts WHERE platform='instagram'")
    for _ in range(3):
        db.execute("UPDATE posts SET not_before=0")
        asyncio.run(publish.run_one())
    p = db.one("SELECT * FROM posts WHERE platform='tiktok'")
    assert p["status"] == "done" and p["remote_id"] == "pub1"
    assert db.one("SELECT status FROM clips WHERE id=?", (cid,))["status"] == "ready"  # a draft isn't "posted"
