"""Unit tests for the parts that are easy to get subtly wrong. Run: .venv/bin/pytest -q"""

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    from cs import config, db
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "test-secret")
    db.reset_for_tests()
    yield
    db.reset_for_tests()


def words_from(text: str, start: float = 0.0, gap_after: dict | None = None):
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.3, 2)})
        t += 0.4 + (gap_after or {}).get(i, 0)
    return out


# ---- transcription chunks ----------------------------------------------------------------------------
def test_chunk_plan_and_merge_drop_overlap_duplicates():
    from cs import groq
    plan = groq.chunk_plan(1250)
    assert plan[0] == (0.0, 600.0) and plan[1][0] == 598.0 and plan[-1][1] == 1250
    a = {"language": "english", "words": [{"word": "x", "start": 597.5, "end": 597.9},
                                          {"word": "dup", "start": 599.2, "end": 599.5}], "segments": []}
    b = {"language": "english", "words": [{"word": "dup", "start": 599.21, "end": 599.5},
                                          {"word": "y", "start": 600.5, "end": 600.9}], "segments": []}
    merged = groq.merge([(0, 600, a), (598, 1198, b)])
    assert [w["word"] for w in merged["words"]] == ["x", "dup", "y"]
    assert groq.chunk_plan(620) == [(0.0, 620)]


# ---- moments --------------------------------------------------------------------------------------------
def test_boundaries_and_snap_to_sentences():
    from cs import moments
    text = ("This is the setup of the story. " * 3 + "And here is the punchline everyone waited for! " * 4).split()
    ws = words_from(" ".join(text))
    segs = [{"text": "x.", "start": 0, "end": ws[6]["end"]}]
    ends = moments.boundaries(ws, segs)
    assert 6 in ends  # "story."
    s, e = moments.snap(9, 30, ws, ends, 5, 60)
    assert s == 7  # back to the start of the sentence containing word 9
    assert ws[e]["word"].endswith((".", "!"))


def test_snap_respects_max_length():
    from cs import moments
    ws = words_from(" ".join(["word"] * 400))  # no punctuation, no pauses: hard cut
    s, e = moments.snap(0, 399, ws, set(), 15, 60)
    assert ws[e]["end"] - ws[s]["start"] + moments.PAD_BEFORE + moments.PAD_AFTER <= 60


def test_music_rule_and_scores():
    from cs import moments
    assert moments.music_ok("none", "none")
    assert not moments.music_ok("faint", "none")
    assert moments.music_ok("faint", "faint")
    assert not moments.music_ok("song", "faint")
    assert not moments.music_ok("", "faint")  # unchecked never passes
    c = moments.Candidate(0, 1, 0, 20, 8, "", "", "", "", loud=3.0)
    loud_and_laugh = moments.Candidate(0, 1, 0, 20, 8, "", "", "", "", loud=3.0, laughter=True)
    text_only = moments.Candidate(0, 1, 0, 20, 8, "", "", "", "")
    assert moments.final_score(loud_and_laugh) > moments.final_score(c) > moments.final_score(text_only)


def test_loud_z_finds_a_spike():
    from cs import moments
    curve = [-30.0 + (i % 3) * 0.5 for i in range(200)]
    curve[150] = -10.0
    z = moments.loud_z(curve)
    assert max(range(len(z)), key=lambda i: z[i]) == 150 and z[150] > 5


def test_drop_overlaps_keeps_the_better_one():
    from cs import moments
    a = moments.Candidate(0, 1, 10, 40, 9, "", "", "", "")
    b = moments.Candidate(0, 1, 15, 45, 6, "", "", "", "")
    c = moments.Candidate(0, 1, 60, 80, 5, "", "", "", "")
    kept = moments.drop_overlaps([b, c, a])
    assert a in kept and c in kept and b not in kept


def test_transcript_lines_cover_word_indices():
    from cs import moments
    ws = words_from("one two three. four five six.")
    segs = [{"text": "one two three.", "start": 0, "end": ws[2]["end"]},
            {"text": "four five six.", "start": ws[3]["start"], "end": ws[5]["end"]}]
    lines = moments.transcript_lines(ws, segs, 0, 5)
    assert lines[0].startswith("[0-2]") and lines[1].startswith("[3-5]")


# ---- captions -------------------------------------------------------------------------------------------
def test_captions_karaoke_and_escaping():
    from cs import captions
    ws = words_from("hello {world} this is a clip. next part here")
    ass = captions.build(ws, 0.0, 5.0, hook="Big {hook}", owner_text="my take", credit="via @me", layout="split")
    assert "{\\c&H0000E5FF&" in ass and "{\\an5}" in ass
    assert "{hook}" not in ass and "HOOK" in ass  # braces removed from user text
    assert "Dialogue: 2,0:00:00.00,0:00:03.20,Hook" in ass
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue: 1")]
    assert len(dialogue) == len(ws)
    for g in captions.chunks(ws):
        assert len(g) <= captions.MAX_WORDS
    assert captions.ts(3661.234) == "1:01:01.23"


# ---- gate -----------------------------------------------------------------------------------------------
def test_gate():
    from cs import db, gate
    st = db.settings()
    assert gate.check(permission="blocked", title="ok", duration=600, live=False, settings=st)
    assert "blocklist" in gate.check(permission="explicit", title="Avengers FULL MOVIE hd", duration=7000, live=False,
                                     settings=st)
    assert gate.check(permission="explicit", title="Podcast Episode 12", duration=3600, live=False, settings=st) == ""
    assert gate.check(permission="campaign", title="NBA Finals recap", duration=900, live=False, settings=st)
    assert gate.check(permission="explicit", title="short", duration=30, live=False, settings=st)
    assert gate.check(permission="explicit", title="live now", duration=0, live=True, settings=st) == ""
    assert gate.check(permission="implied", title="Podcast", duration=3000, live=False, settings=st) == ""
    assert gate.check(permission="nonsense", title="Podcast", duration=3000, live=False, settings=st)
    assert gate.check(permission="explicit", title="x", duration=200, live=False, settings=st, min_minutes=5)


# ---- webhooks -------------------------------------------------------------------------------------------
def test_websub_signature_and_atom():
    from cs import yt
    body = b"""<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
      <entry><yt:videoId>abc123def45</yt:videoId><yt:channelId>UCxxxxxxxxxxxxxxxxxxxxxx</yt:channelId>
      <title>T</title><published>2026-10-04T01:00:00+00:00</published></entry></feed>"""
    sig = hmac.new(yt.channel_secret("UCx").encode(), body, hashlib.sha1).hexdigest()
    assert yt.verify_signature("UCx", body, f"sha1={sig}")
    assert not yt.verify_signature("UCx", body + b" ", f"sha1={sig}")
    assert not yt.verify_signature("UCy", body, f"sha1={sig}")
    assert not yt.verify_signature("UCx", body, "")
    assert yt.parse_atom(body)[0]["video_id"] == "abc123def45"
    assert yt.parse_atom(b"not xml") == []
    assert yt.iso_duration("PT1H2M3S") == 3723 and yt.iso_duration("P0D") == 0


def test_twitch_signature():
    from cs import twitch
    body = b'{"challenge":"x"}'
    h = {"twitch-eventsub-message-id": "m1", "twitch-eventsub-message-timestamp": "2026-10-04T01:00:00.123Z"}
    h["twitch-eventsub-message-signature"] = "sha256=" + hmac.new(
        twitch.secret().encode(), b"m1" + h["twitch-eventsub-message-timestamp"].encode() + body, hashlib.sha256).hexdigest()
    assert twitch.verify(h, body)
    assert not twitch.verify({**h, "twitch-eventsub-message-id": "m2"}, body)
    assert twitch.vod_seconds("3h8m33s") == 11313


# ---- framing --------------------------------------------------------------------------------------------
def test_layout_choice_and_smooth_track():
    from cs import faces, render
    one = [(i / 4, [(0.30 + (i % 2) * 0.01, 0.4, 0.1, 0.18, 0.9)]) for i in range(40)]
    assert faces.choose_layout(one, 1920, 1080) == "track"
    assert faces.choose_layout([(i / 4, []) for i in range(40)], 1920, 1080) == "general"
    two = [(i / 4, [(0.25, 0.4, 0.1, 0.18, 0.9), (0.75, 0.4, 0.1, 0.17, 0.9)]) for i in range(40)]
    assert faces.choose_layout(two, 1920, 1080) == "split"
    assert faces.choose_layout(one, 1080, 1920) == "vertical"
    path = faces.track_path(one, 1920, 1080)
    xs = {x for _, x in path}
    assert len(xs) <= 3  # jitter of 1% is absorbed by the dead zone
    cut = one[:20] + [(5 + i / 4, [(0.8, 0.4, 0.1, 0.18, 0.9)]) for i in range(20)]
    p2 = faces.track_path(cut, 1920, 1080)
    assert p2[-1][1] > p2[0][1] + 500  # a lasting change (cut) is followed
    cw = 1080 * 9 / 16
    assert all(0 <= x <= 1920 - cw for _, x in p2)
    lines = render.sendcmd_lines(p2, 606, 30)
    assert lines.startswith("0.0000 crop x ") and lines.count("\n") >= 30 * p2[-1][0]  # one update per frame
    xs = [int(l.split()[3].rstrip(";")) for l in lines.splitlines()]
    steps = [abs(b - a) for a, b in zip(xs, xs[1:])]
    assert max(steps) > 300 and sorted(steps)[-2] < 40  # the cut jumps once; everything else glides
    jitter = [(i / 4, 600 + (i % 2) * 30) for i in range(40)]  # face wobbling ±30 px: camera holds still
    assert len({l.split()[3] for l in render.sendcmd_lines(jitter, 606, 30).splitlines()}) == 1


# ---- database -------------------------------------------------------------------------------------------
def test_db_settings_usage_seen():
    from cs import db
    assert db.settings()["clips_per_hour"] == 8
    db.kv_set("settings", {"clips_per_hour": 3})
    assert db.settings()["clips_per_hour"] == 3 and db.settings()["min_clip_seconds"] == 15
    db.usage_add("groq_seconds", 10)
    db.usage_add("groq_seconds", 5)
    assert db.usage_get("groq_seconds") == 15
    assert db.mark_seen("youtube", "v1") and not db.mark_seen("youtube", "v1")


def test_cleanup_rules(tmp_path, monkeypatch):
    from cs import config, db, pipeline
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    now = db.now()

    def source(status, age_days):
        sid = db.insert("sources", {"status": status, "created_at": now - age_days * 86400, "updated_at": now})
        wd = pipeline.work_dir(sid)
        wd.mkdir(parents=True)
        (wd / "source.mkv").write_bytes(b"x")
        (wd / "transcript.json").write_text("{}")
        return sid

    decided = source("review", 1)
    db.insert("clips", {"source_id": decided, "start": 0, "end": 1, "status": "rejected", "created_at": now, "updated_at": now})
    open_ = source("review", 1)
    db.insert("clips", {"source_id": open_, "start": 0, "end": 1, "status": "review", "created_at": now, "updated_at": now})
    old = source("review", 8)
    oc = db.insert("clips", {"source_id": old, "start": 0, "end": 1, "status": "review", "created_at": now, "updated_at": now})
    pipeline.cleanup()
    assert not (pipeline.work_dir(decided) / "source.mkv").exists()
    assert (pipeline.work_dir(decided) / "transcript.json").exists()
    assert db.one("SELECT status, files_deleted FROM sources WHERE id=?", (decided,)) == {"status": "done", "files_deleted": 1}
    assert (pipeline.work_dir(open_) / "source.mkv").exists()
    assert db.one("SELECT status FROM clips WHERE id=?", (oc,))["status"] == "expired"
