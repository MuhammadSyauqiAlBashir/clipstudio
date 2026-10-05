"""Campaign catalogue: normalising Clippo data, flags, keeping ended campaigns, hashtags in captions, footage import."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

RAW = {"id": "c1", "title": "OST FILOSOFI TERAS - MENIPU DIRI", "hashtags": ["#FilosofiTeras", "MenipuDiri"],
       "assetVideoUrlsV2": [{"url": "https://www.youtube.com/watch?v=abcdefghijk", "isRequired": True, "displayText": "main"},
                            {"url": "https://drive.google.com/drive/folders/x", "isRequired": False}],
       "platformSupported": [0, 1], "ratePerKView": 3000, "rateUnit": 1000, "budgetPercentage": 93.5,
       "clippersJoinedCount": 4232, "requirementsV2": "<p>Platforms: TikTok</p><p>Diakhir video wajib ada teks &quot;X&quot;</p>",
       "creator": {"user": {"name": "MD ENTERTAINMENT"}}, "imageUrl": "https://img/x.png", "isYellowCart": False}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    from cs import config, db, push
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 0)

    async def no_push(*a, **k):
        return 0
    monkeypatch.setattr(push, "send", no_push)
    db.reset_for_tests()
    yield
    db.reset_for_tests()


def test_normalize_and_flags():
    from cs import campaigns
    c = campaigns.normalize_clippo(RAW)
    assert c["hashtags"] == ["#FilosofiTeras", "#MenipuDiri"]
    assert c["platforms"] == ["tiktok", "instagram"] and c["rate"] == 3000
    assert c["brief"].splitlines()[0] == "Platforms: TikTok" and '"X"' in c["brief"] and "<p>" not in c["brief"]
    assert {"needs_audio", "custom_edit", "budget_low"} <= set(c["flags"])
    assert c["url"] == "https://app.clippo.id/campaigns/c1"
    assert campaigns.youtube_footage(c) == ["https://www.youtube.com/watch?v=abcdefghijk"]
    fb = campaigns.normalize_clippo({**RAW, "id": "c2", "title": "Bunda", "requirementsV2": "", "platformSupported": [2],
                                     "budgetPercentage": 10})
    assert fb["flags"] == [] and fb["platforms"] == ["facebook"]  # Facebook is one of the owner's platforms now
    other = campaigns.normalize_clippo({**RAW, "id": "c3", "title": "X", "requirementsV2": "", "platformSupported": [7],
                                        "budgetPercentage": 10})
    assert other["flags"] == ["other_platforms"]


def test_upsert_keeps_ended_and_reports_new(monkeypatch):
    from cs import campaigns, db
    a = campaigns.normalize_clippo(RAW)
    b = campaigns.normalize_clippo({**RAW, "id": "c2", "title": "Second"})
    assert len(campaigns.upsert([a, b], "clippo")) == 2
    assert campaigns.upsert([a], "clippo") == []
    assert db.one("SELECT status FROM campaigns WHERE ext_id='c2'")["status"] == "ended"  # kept, marked ended
    assert db.one("SELECT COUNT(*) n FROM campaigns")["n"] == 2

    async def fake_fetch():
        return [a, b, campaigns.normalize_clippo({**RAW, "id": "c3"})]
    monkeypatch.setattr(campaigns, "fetch_clippo", fake_fetch)
    assert asyncio.run(campaigns.refresh()) == 1
    assert db.one("SELECT status FROM campaigns WHERE ext_id='c2'")["status"] == "open"


def test_campaign_flow_hashtags_and_submit():
    from fastapi.testclient import TestClient

    from cs import campaigns, db, main, posttext
    campaigns.upsert([campaigns.normalize_clippo(RAW)], "clippo")
    camp = db.one("SELECT id FROM campaigns")["id"]
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        r = c.post(f"/api/campaigns/{camp}/clip").json()
        assert r["added"] == ["https://www.youtube.com/watch?v=abcdefghijk"] and len(r["manual"]) == 1
        assert c.post(f"/api/campaigns/{camp}/clip").json()["added"] == []  # never twice
        src = db.one("SELECT * FROM sources")
        assert src["campaign_id"] == camp and src["permission"] == "campaign"
        now = db.now()
        cid = db.insert("clips", {"source_id": src["id"], "start": 0, "end": 20, "status": "posted", "hook": "H",
                                  "hashtags": "#lucu", "posted": '{"instagram": "https://instagram.com/reel/x", "tiktok": "posted"}',
                                  "created_at": now, "updated_at": now})
        cap = posttext.post_caption(db.one("SELECT * FROM clips WHERE id=?", (cid,)), src, "instagram")
        assert cap.split("\n")[-1].startswith("#FilosofiTeras #MenipuDiri #lucu")
        items = c.get(f"/api/campaigns/{camp}/submit").json()["items"]
        assert [i["platform"] for i in items] == ["instagram"]  # 'posted' without a link isn't submittable
        assert c.get("/api/campaigns").json()["campaigns"][0]["to_submit"] == 1
        c.post(f"/api/campaigns/{camp}/submitted", json={"items": [{"clip_id": cid, "platform": "instagram"}]})
        assert c.get("/api/campaigns").json()["campaigns"][0]["to_submit"] == 0
        assert c.patch(f"/api/campaigns/{camp}", json={"joined": True, "hidden": True}).json()["campaign"]["joined"] == 1
        assert c.get("/api/campaigns?show=open").json()["campaigns"] == []
        assert len(c.get("/api/campaigns?show=hidden").json()["campaigns"]) == 1
    main.app.dependency_overrides.clear()


def test_clippo_join_and_auto_submit(monkeypatch):
    from fastapi.testclient import TestClient

    from cs import campaigns, clippo, db, main
    campaigns.upsert([campaigns.normalize_clippo({**RAW, "requirementsV2": "", "budgetPercentage": 10})], "clippo")
    camp = db.one("SELECT * FROM campaigns")
    monkeypatch.setattr(clippo, "configured", lambda: True)
    joined, submitted = [], []

    async def fake_join(ext):
        joined.append(ext)
        return {"status": 200}

    async def fake_check(ext, urls):
        return [{"success": True, "data": {"platform": "INSTAGRAM", "caption": "cap", "postedAt": "2026-10-05T00:00:00Z",
                                           "video": {"thumbnailUrl": "t"}, "eligibility": {"eligible": "ig" in u}}}
                for u in urls]

    async def fake_submit(ext, items):
        submitted.extend(items)
        return {"status": 200}
    monkeypatch.setattr(clippo, "join", fake_join)
    monkeypatch.setattr(clippo, "bulk_check", fake_check)
    monkeypatch.setattr(clippo, "submit", fake_submit)
    main.app.dependency_overrides[main.current] = lambda: main.Session({"username": "bashirsyauqi"}, "t")
    with TestClient(main.app, headers={"X-CS": "1"}) as c:
        assert c.post(f"/api/campaigns/{camp['id']}/join").json()["campaign"]["joined"] == 1
        assert joined == ["c1"]
        now = db.now()
        sid = db.insert("sources", {"campaign_id": camp["id"], "created_at": now, "updated_at": now})
        cid = db.insert("clips", {"source_id": sid, "start": 0, "end": 20, "status": "posted", "created_at": now,
                                  "updated_at": now,
                                  "posted": '{"instagram": "https://instagram.com/reel/ig1", "tiktok": "https://tiktok.com/v/x2", "youtube": "https://youtu.be/no"}'})
        assert asyncio.run(campaigns.auto_submit()) == 1
        assert [i["videoUrl"] for i in submitted] == ["https://instagram.com/reel/ig1"]  # youtube isn't a campaign platform
        assert submitted[0]["clipTitle"] == "cap" and submitted[0]["thumbnailUrl"] == "t"
        sub = db.jload(db.one("SELECT submitted FROM clips WHERE id=?", (cid,))["submitted"])
        assert "instagram" in sub and "tiktok" not in sub
        items = {i["platform"]: i for i in c.get(f"/api/campaigns/{camp['id']}/submit").json()["items"]}
        assert items["tiktok"]["waiting"] and not items["instagram"]["waiting"]
        assert asyncio.run(campaigns.auto_submit()) == 0  # never twice
    main.app.dependency_overrides.clear()
