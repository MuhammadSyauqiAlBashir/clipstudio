"""API flow tests with the login replaced by a fake session (PocketBase isn't needed)."""

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import config, db, main
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "CLIPS_DIR", tmp_path / "clips")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 0)
    db.reset_for_tests()
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        yield c
    main.app.dependency_overrides.clear()
    db.reset_for_tests()


def test_requires_header_and_login(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cs import config, db, main
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.reset_for_tests()
    with TestClient(main.app) as c:
        assert c.post("/api/sources", json={}).status_code == 403  # no X-CS header
        assert c.get("/api/sources").status_code == 401
        assert c.get("/api/health").json() == {"ok": True}
    db.reset_for_tests()


def test_allowed_users_only():
    from cs import main
    assert main.allowed({"username": "bashirsyauqi", "role": "admin"})
    assert main.allowed({"username": "bells", "role": ""})
    assert not main.allowed({"username": "rafi", "role": ""})
    assert not main.allowed({"username": "bells", "role": "games"})  # machine logins never


def test_add_source_and_clip_flow(client):
    from cs import db
    r = client.post("/api/sources", json={"url": "https://www.youtube.com/watch?v=abc", "permission": "blocked"})
    assert r.status_code == 400
    r = client.post("/api/sources", json={"url": "https://www.youtube.com/watch?v=abc", "permission": "explicit",
                                          "proof": "creator post"})
    assert r.status_code == 200
    sid = r.json()["source"]["id"]
    job = db.one("SELECT * FROM jobs WHERE source_id=?", (sid,))
    assert job["kind"] == "process" and job["priority"] == 20

    now = db.now()
    cid = db.insert("clips", {"source_id": sid, "start": 10, "end": 40, "status": "review", "hook": "Big moment",
                              "caption": "Cap", "hashtags": "#a", "music": "none", "created_at": now, "updated_at": now})
    lst = client.get("/api/clips?status=review").json()
    assert lst["clips"][0]["id"] == cid and "TikTok" not in lst["clips"][0]["captions"]["tiktok"]
    assert "#shorts" in lst["clips"][0]["captions"]["youtube"]

    assert client.post(f"/api/clips/{cid}/approve").status_code == 200
    assert db.one("SELECT * FROM jobs WHERE clip_id=?", (cid,))["priority"] == 30
    assert client.post(f"/api/clips/{cid}/approve").status_code == 400  # already approved

    # once the final exists, changing on-screen text queues a new final
    db.update("clips", cid, {"status": "ready", "final": "/nonexistent/final.mp4"})
    r = client.patch(f"/api/clips/{cid}", json={"owner_text": "my take"})
    assert r.json()["clip"]["status"] == "approved"
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE clip_id=?", (cid,))["n"] == 2
    db.update("clips", cid, {"status": "ready"})
    r = client.post(f"/api/clips/{cid}/posted", json={"platform": "tiktok", "url": "https://tiktok.com/x"})
    assert r.json()["clip"]["status"] == "posted"

    assert client.delete(f"/api/sources/{sid}").status_code == 200
    assert db.one("SELECT 1 FROM clips WHERE id=?", (cid,)) is None


def test_media_paths_are_confined(client):
    from cs import db
    now = db.now()
    cid = db.insert("clips", {"source_id": 1, "start": 0, "end": 1, "preview": "/etc/passwd", "created_at": now,
                              "updated_at": now})
    assert client.get(f"/api/clips/{cid}/preview.mp4").status_code == 404


def test_settings_validation(client):
    assert client.put("/api/settings", json={"min_clip_seconds": 50, "max_clip_seconds": 40}).status_code == 400
    assert client.put("/api/settings", json={"caption_template": "{nope}"}).status_code == 400
    r = client.put("/api/settings", json={"clips_per_hour": 5, "music_allowed": "faint"})
    assert r.json()["settings"]["clips_per_hour"] == 5


def test_websub_flow(client):
    from cs import db, yt
    cid = db.insert("channels", {"platform": "youtube", "ext_id": "UCabcdefghijklmnopqrstuv", "handle": "x",
                                 "title": "X", "permission": "explicit", "created_at": db.now()})
    t = yt.topic("UCabcdefghijklmnopqrstuv")
    r = client.get(f"/api/websub/callback?c={cid}&hub.mode=subscribe&hub.topic={t}&hub.challenge=abc&hub.lease_seconds=100")
    assert r.status_code == 200 and r.text == "abc"
    assert db.jload(db.one("SELECT sub FROM channels WHERE id=?", (cid,))["sub"])["verified"]
    assert client.get(f"/api/websub/callback?c={cid}&hub.mode=subscribe&hub.topic=other&hub.challenge=abc").status_code == 404
    body = (b'<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom"><entry>'
            b'<yt:videoId>vid12345678</yt:videoId><yt:channelId>UCabcdefghijklmnopqrstuv</yt:channelId></entry></feed>')
    # unsigned: dropped
    client.post(f"/api/websub/callback?c={cid}", content=body, headers={"X-CS": ""})
    assert db.one("SELECT COUNT(*) n FROM inbox")["n"] == 0
    sig = hmac.new(yt.channel_secret("UCabcdefghijklmnopqrstuv").encode(), body, hashlib.sha1).hexdigest()
    client.post(f"/api/websub/callback?c={cid}", content=body, headers={"X-Hub-Signature": f"sha1={sig}", "X-CS": ""})
    assert db.one("SELECT COUNT(*) n FROM inbox")["n"] == 1


def test_twitch_callback(client):
    import json
    import time

    from cs import twitch
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    body = json.dumps({"challenge": "xyz", "subscription": {"type": "stream.online"}}).encode()

    def headers(mid, kind):
        sig = "sha256=" + hmac.new(twitch.secret().encode(), mid.encode() + ts.encode() + body, hashlib.sha256).hexdigest()
        return {"Twitch-Eventsub-Message-Id": mid, "Twitch-Eventsub-Message-Timestamp": ts,
                "Twitch-Eventsub-Message-Signature": sig, "Twitch-Eventsub-Message-Type": kind, "X-CS": ""}

    r = client.post("/api/twitch/callback", content=body, headers=headers("m1", "webhook_callback_verification"))
    assert r.status_code == 200 and r.text == "xyz"
    bad = headers("m2", "notification")
    bad["Twitch-Eventsub-Message-Signature"] = "sha256=00"
    assert client.post("/api/twitch/callback", content=body, headers=bad).status_code == 403


def test_browse_marks_added_and_blocks_duplicates(client, monkeypatch):
    from cs import yt

    async def find(q):
        return ("UCabc", []) if "@" in q else ("", [{"id": "UCx", "title": "X", "thumb": "", "description": ""}])

    async def page(cid, token="", tab="videos"):
        return {"channel": {"id": cid, "title": "Chan", "handle": "chan", "thumb": "", "subscribers": 1, "video_count": 2,
                            "url": "u", "uploads": "UU", "at": 0},
                "videos": [{"id": "abcdefghijk", "title": "V1", "duration": 3000, "views": 5, "state": "none", "thumb": "",
                            "published": "2026-01-01T00:00:00Z", "channel_id": cid}],
                "next": "N", "prev": "", "total": 30}
    monkeypatch.setattr(yt, "find_channels", find)
    monkeypatch.setattr(yt, "uploads_page", page)
    assert client.get("/api/browse/youtube?q=name").json()["choices"][0]["id"] == "UCx"
    r = client.get("/api/browse/youtube?q=@chan").json()
    assert r["videos"][0]["added"] is None and r["next"] == "N"
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    assert client.post("/api/sources", json={"url": url, "permission": "implied"}).status_code == 200
    assert client.get("/api/browse/youtube?channel=UCabc").json()["videos"][0]["added"]["status"] == "queued"
    assert client.post("/api/sources", json={"url": "https://youtu.be/abcdefghijk", "permission": "implied"}).status_code == 409
