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
    db.kv_set("settings", {"schedule_on": False})  # these tests post right away; the schedule has its own tests
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
        h = c.head(path)  # Meta's download proxy asks for the headers first
        assert h.status_code == 200 and h.headers["content-length"] == "5"
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

    async def fake_finish(code, app=None):
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


def test_youtube_connect_upload_and_limits(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import config, db, main, publish, youtube
    monkeypatch.setattr(config, "YT_OAUTH_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "YT_OAUTH_CLIENT_SECRET", "sec")
    url = youtube.authorize_url()
    assert "youtube.upload" in url and "access_type=offline" in url and "redirect_uri=https%3A%2F%2Fclips" in url

    async def fake_finish(code):
        db.kv_set("youtube_auth", {"access_token": "a", "refresh_token": "r", "expires": time.time() + 3000})
    monkeypatch.setattr(youtube, "finish_login", fake_finish)
    with TestClient(main.app, follow_redirects=False) as c:
        assert "failed" in c.get("/api/youtube/oauth?code=x&state=bad").headers["location"]
        st = youtube.authorize_url().split("state=")[1]
        assert c.get(f"/api/youtube/oauth?code=x&state={st}").headers["location"] == "/#more?youtube=connected"
    assert youtube.connected() and publish.connected("youtube")

    calls = []

    async def fake_upload(path, title, desc, tags, lang=""):
        calls.append((title, tags))
        return {"id": "vid1", "url": "https://www.youtube.com/shorts/vid1", "privacy": "private",
                "upload_status": "uploaded", "channel": "bashclipeveryday"}
    monkeypatch.setattr(youtube, "upload", fake_upload)
    cid = make_clip(state)
    db.execute("INSERT INTO posts(clip_id, platform, status, created_at, updated_at) VALUES(?,?,?,?,?)",
               (cid, "youtube", "queued", db.now(), db.now()))
    assert asyncio.run(publish.run_one())
    p = db.one("SELECT * FROM posts WHERE platform='youtube'")
    assert p["status"] == "done" and p["url"].endswith("vid1") and calls[0][0] == "Hook" and "#shorts" in calls[0][1]
    db.execute("UPDATE posts SET status='queued'")  # a retry never uploads twice
    asyncio.run(publish.run_one())
    assert len(calls) == 1
    db.usage_add("yt_uploads", youtube.DAILY_UPLOADS, db.google_day())  # counted on Google's day
    assert youtube.uploads_left() == 0
    db.execute("DELETE FROM usage")
    db.usage_add("yt_units", 9000, db.google_day())  # the shared budget also limits uploads
    assert youtube.uploads_left() == 0
    db.execute("DELETE FROM usage")
    db.usage_add("yt_units", 9000, "1999-01-01")      # yesterday's (Google day) usage doesn't count
    assert youtube.uploads_left() == youtube.DAILY_UPLOADS


def test_facebook_reel_flow(state, monkeypatch):
    from cs import config, db, facebook, publish
    monkeypatch.setattr(config, "FB_PAGE_ID", "123")
    monkeypatch.setattr(config, "FB_PAGE_TOKEN", "pt")
    assert publish.autopost_enabled("facebook")  # on by default even with older saved settings
    calls = []

    async def up(path):
        calls.append("upload")
        return "v9"

    async def fin(vid, desc, state="PUBLISHED"):
        calls.append(("finish", state, "#reels" in desc))
    seq = iter([("processing", "not_started", ""), ("ready", "complete", "https://www.facebook.com/reel/v9")])

    async def stat(vid):
        return next(seq)
    monkeypatch.setattr(facebook, "start_and_upload", up)
    monkeypatch.setattr(facebook, "finish", fin)
    monkeypatch.setattr(facebook, "status", stat)
    cid = make_clip(state)
    db.execute("INSERT INTO posts(clip_id, platform, status, created_at, updated_at) VALUES(?,?,?,?,?)",
               (cid, "facebook", "queued", db.now(), db.now()))
    for _ in range(3):
        db.execute("UPDATE posts SET not_before=0")
        asyncio.run(publish.run_one())
    p = db.one("SELECT * FROM posts WHERE platform='facebook'")
    assert p["status"] == "done" and p["url"].endswith("/reel/v9")
    assert calls == ["upload", ("finish", "PUBLISHED", True)]  # uploaded once, published once


def test_failures_stop_at_once_with_a_named_reason(state, monkeypatch):
    import httpx

    from cs import db, publish
    cid = make_clip(state)
    publish.queue_for(cid)

    async def limited(post, clip, caption):  # even a network-type error is not retried by itself any more
        raise httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(publish, "instagram", limited)
    assert asyncio.run(publish.run_one())
    p = db.one("SELECT status, error FROM posts WHERE clip_id=?", (cid,))
    assert p["status"] == "failed" and "timed out" in p["error"]
    assert not asyncio.run(publish.run_one())  # nothing is tried again until the owner taps Retry
    ex = publish.explain
    assert ex("facebook", "Facebook 400: We limit how often you can post").startswith("Facebook posting limit")
    assert ex("tiktok", "TikTok 400: spam_risk_too_many_pending_share").startswith("TikTok inbox full")
    assert ex("instagram", '{"error":{"message":"API access blocked."}}').startswith("Meta blocked the app")
    assert ex("facebook", "Cannot call API for app 1 on behalf of user 2").startswith("Meta blocked the app")
    assert ex("instagram", "Instagram rejected the video: Video download failed (Fwdproxy)").startswith("Instagram couldn't fetch")
    assert "Retry" in ex("youtube", "something new")


def test_single_post_and_clip_pause(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import db, main, publish, worker
    cid = make_clip(state)
    publish.queue_for(cid)
    pid = db.one("SELECT id FROM posts WHERE clip_id=?", (cid,))["id"]
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        assert c.put(f"/api/posts/{pid}/pause", json={"paused": True}).status_code == 200
        q = c.get("/api/queue").json()
        row = next(p for p in q["posts"] if p["id"] == pid)
        assert row["paused"] and row["can_pause"] and "this post" in row["state"]
        assert not asyncio.run(publish.run_one())  # a paused post is skipped
        assert c.put(f"/api/posts/{pid}/pause", json={"paused": False}).status_code == 200
        # one clip's final render can be held too
        now = db.now()
        a = db.insert("clips", {"source_id": db.one("SELECT source_id FROM clips WHERE id=?", (cid,))["source_id"], "start": 0,
                                "end": 20, "status": "approved", "created_at": now, "updated_at": now})
        db.enqueue("final", clip_id=a, priority=30)
        assert c.put(f"/api/clips/{a}/pause", json={"paused": True}).status_code == 200
        assert worker.next_job(("final",)) is None
        fin = next(f for f in c.get("/api/queue").json()["finals"] if f["clip_id"] == a)
        assert fin["paused"] and "this clip" in fin["state"]
        c.put(f"/api/clips/{a}/pause", json={"paused": False})
        assert worker.next_job(("final",))["clip_id"] == a
    main.app.dependency_overrides.clear()


def test_tiktok_direct_post_rules_and_flow(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import autopilot, config, db, main, publish, tiktok
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "prodkey")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "s1")
    monkeypatch.setattr(config, "TIKTOK_SANDBOX_CLIENT_KEY", "sbkey")
    monkeypatch.setattr(config, "TIKTOK_SANDBOX_CLIENT_SECRET", "s2")
    # production asks only for what's approved; the sandbox for everything; the login link names its own key
    assert "video.publish" not in tiktok.authorize_url() and "client_key=prodkey" in tiktok.authorize_url()
    db.kv_set("settings", {"schedule_on": False, "tiktok_app": "sandbox"})
    url = tiktok.authorize_url()
    assert "client_key=sbkey" in url and "video.publish" in url and "video.list" in url
    assert tiktok.check_state(url.split("state=")[1]) == "sandbox"
    # each app keeps its own login
    db.kv_set("tiktok_auth_sandbox", {"access_token": "a", "refresh_token": "r", "expires": time.time() + 9999,
                                      "scope": "user.info.basic,video.upload,video.publish,video.list"})
    assert tiktok.connected() and not tiktok.connected("production") and not tiktok.direct_mode()
    db.kv_set("settings", {"schedule_on": False, "tiktok_app": "sandbox", "tiktok_direct": True})
    assert tiktok.direct_mode() and tiktok.private_only()

    cid = make_clip(state)
    publish.queue_for(cid)  # Direct Post is never queued by itself: it needs the owner's choices
    assert not db.one("SELECT 1 FROM posts WHERE platform='tiktok'")

    async def creator():
        return {"creator_username": "bashclipeveryday", "creator_nickname": "Bash", "creator_avatar_url": "",
                "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "comment_disabled": False,
                "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 600}
    monkeypatch.setattr(tiktok, "creator_info", creator)
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    base = {"clip_ids": [cid], "privacy_level": "SELF_ONLY", "consent": True}
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        info = c.get("/api/tiktok/creator").json()
        assert info["username"] == "bashclipeveryday" and info["duet_disabled"] and info["private_only"]
        assert [p["allowed"] for p in info["privacy"]] == [False, True]  # test app: only "Only me"
        assert c.post("/api/tiktok/post", json={**base, "consent": False}).status_code == 400
        assert c.post("/api/tiktok/post", json={**base, "privacy_level": "PUBLIC_TO_EVERYONE"}).status_code == 400
        assert c.post("/api/tiktok/post", json={**base, "disclose": True}).status_code == 400
        assert c.post("/api/tiktok/post", json={**base, "disclose": True, "branded": True}).status_code == 400
        r = c.post("/api/tiktok/post", json={**base, "title": "My caption", "allow_duet": True, "allow_comment": True}).json()
        assert r["done"] == [cid]
    main.app.dependency_overrides.clear()
    p = db.one("SELECT * FROM posts WHERE platform='tiktok'")
    opts = db.jload(p["options"], {})
    assert opts["mode"] == "direct" and opts["title"] == "My caption" and opts["allow_comment"]
    assert not opts["allow_duet"]  # the account has duets off: never sent as allowed
    info_ = tiktok.post_info(opts)
    assert info_["privacy_level"] == "SELF_ONLY" and info_["disable_duet"] and not info_["disable_comment"]

    sent = {}

    async def direct(path, o):
        sent.update(o)
        return "pub9"
    seq = iter([{"status": "PROCESSING_UPLOAD"}, {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []}])

    async def full(pid):
        return next(seq)
    monkeypatch.setattr(tiktok, "direct_post", direct)
    monkeypatch.setattr(tiktok, "status_full", full)
    db.execute("DELETE FROM posts WHERE platform!='tiktok'")
    for _ in range(3):
        db.execute("UPDATE posts SET not_before=0")
        asyncio.run(publish.run_one())
    p = db.one("SELECT * FROM posts WHERE platform='tiktok'")
    assert p["status"] == "done" and sent["privacy_level"] == "SELF_ONLY"
    assert db.one("SELECT status FROM clips WHERE id=?", (cid,))["status"] == "ready"  # private: not "posted"

    # views come from video.list, matched by the video id in the link
    db.execute("UPDATE posts SET url='https://www.tiktok.com/@bashclipeveryday/video/7123', posted_at=?", (time.time(),))

    async def vids(pages=5):
        return [{"id": 7123, "view_count": 1500, "like_count": 90, "comment_count": 4, "share_count": 2}]
    monkeypatch.setattr(tiktok, "my_videos", vids)
    asyncio.run(autopilot.tiktok_stats())
    assert db.jload(db.one("SELECT stats FROM posts WHERE platform='tiktok'")["stats"], {})["views"] == 1500
    assert tiktok.video_id("https://www.tiktok.com/@x/video/99?lang=en") == "99" and tiktok.video_id("nope") == ""
    assert tiktok.avatar_allowed("https://p16-sign-va.tiktokcdn.com/a.jpeg") and not tiktok.avatar_allowed("https://evil.com/x")
    assert "Private account ON" in publish.explain("tiktok", "TikTok 403: unaudited_client_can_only_post_to_private_accounts")


def test_tiktok_videos_endpoint(state, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import config, db, main, tiktok
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "s")
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        assert c.get("/api/tiktok/videos").json() == {"available": False, "videos": []}
        db.kv_set("tiktok_auth", {"access_token": "a", "refresh_token": "r", "expires": time.time() + 9999,
                                  "scope": "user.info.basic,video.upload,video.list"})

        async def vids(pages=5):
            return [{"id": 1, "title": "Sushi", "view_count": 321, "like_count": 9, "share_url": "https://www.tiktok.com/@x/video/1"}]
        monkeypatch.setattr(tiktok, "my_videos", vids)
        r = c.get("/api/tiktok/videos").json()
        assert r["available"] and r["videos"][0]["views"] == 321 and r["videos"][0]["id"] == "1"
    main.app.dependency_overrides.clear()
