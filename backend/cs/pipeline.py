"""The clip pipeline, one step after another, resumable (every step leaves a file in the source's work dir):
download → audio → transcript (Groq) → loudness → moments (Gemini) → music/laughter check (Gemini audio) →
face plan + 540x960 preview per clip → review. Final 1080x1920 renders run only for approved clips."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import time
from pathlib import Path

from . import ai, captions, config, db, faces, fetch, gate, groq, media, moments, publish, push, render

log = logging.getLogger("cs.pipeline")


class Wait(Exception):
    """Pause the job until `until` (quota, disk, AI busy) without counting it as a failure."""

    def __init__(self, until: float, reason: str):
        super().__init__(reason)
        self.until = until
        self.reason = reason


class Refused(Exception):
    """The source must not be processed (gate). Not retried."""


def work_dir(source_id: int) -> Path:
    return config.WORK_DIR / f"s{source_id}"


def clip_dir(source_id: int, clip_id: int) -> Path:
    return work_dir(source_id) / "clips" / f"c{clip_id}"


def final_path(clip_id: int) -> Path:
    return config.CLIPS_DIR / f"clip-{clip_id}.mp4"


def free_gb() -> float:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(config.STATE_DIR).free / 1e9


def disk_guard():
    if free_gb() < config.MIN_FREE_DISK_GB:
        raise Wait(time.time() + 1800, f"Less than {config.MIN_FREE_DISK_GB:g} GB free disk; waiting for cleanup")


def set_source(sid: int, **kw):
    kw["updated_at"] = db.now()
    db.update("sources", sid, kw)


def progress_cb(sid: int, step: str, lo: float, hi: float):
    last = [0.0]

    def cb(frac: float):
        now = time.monotonic()
        if now - last[0] > 2:
            last[0] = now
            set_source(sid, step=step, progress=round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 3))
    return cb


def credit_of(src: dict) -> str:
    url = src.get("creator_url") or ""
    m = re.search(r"youtube\.com/@([\w.\-]+)", url) or re.search(r"(?:twitch\.tv|kick\.com)/([\w]+)", url)
    if m:
        return f"via @{m.group(1)}"
    return f"via {src['creator']}" if src.get("creator") else ""


# ---------------------------------------------------------------------------------------------------------
# Process a source
# ---------------------------------------------------------------------------------------------------------
async def process(source_id: int):
    src = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not src:
        raise Refused("source deleted")
    if src["files_deleted"]:
        raise Refused("the source video was already deleted")
    wd = work_dir(source_id)
    wd.mkdir(parents=True, exist_ok=True)
    st = db.settings()
    max_s = float(st["max_source_hours"]) * 3600

    # 1. details + gate + download
    video = Path(src["file"]) if src["file"] else None
    if not video or not video.exists():
        if src["kind"] == "upload":
            raise Refused("the uploaded file is missing")
        disk_guard()
        set_source(source_id, status="downloading", step="details", progress=0)
        meta = await fetch.info(src["url"], wd)
        if meta["live_status"] in ("is_live", "is_upcoming"):
            raise Refused("this is a live stream: it is recorded by the watcher, not downloaded")
        set_source(source_id, title=meta["title"][:300], creator=src["creator"] or meta["creator"][:120],
                   creator_url=src["creator_url"] or meta["creator_url"], duration=meta["duration"],
                   video_id=meta["id"], platform=src["platform"] or meta["platform"])
        reason = gate.check(permission=src["permission"], title=meta["title"], duration=meta["duration"], live=False,
                            settings=st)
        if reason:
            set_source(source_id, status="rejected_by_gate", reason=reason)
            db.event(f"Gate refused “{meta['title'][:80]}”: {reason}", "gate", "warn", source_id)
            raise Refused(reason)
        set_source(source_id, step="downloading")
        video = await fetch.download(src["url"], wd, max_s, meta["duration"], progress_cb(source_id, "downloading", 0, 0.25))
        set_source(source_id, file=str(video), size=video.stat().st_size)
        src = db.one("SELECT * FROM sources WHERE id=?", (source_id,))

    info = await asyncio.to_thread(media.probe, video)
    duration = min(info["duration"], max_s) if info["duration"] else max_s
    if not info["has_audio"]:
        raise Refused("the video has no sound")
    set_source(source_id, duration=duration)

    # 2. audio
    audio = wd / "audio.mp3"
    if not audio.exists():
        set_source(source_id, status="transcribing", step="audio", progress=0.25)
        tmp = wd / "audio.part.mp3"
        await asyncio.to_thread(media.extract_audio, video, tmp, duration)
        tmp.replace(audio)

    # 3. transcript
    tpath = wd / "transcript.json"
    if tpath.exists():
        transcript = json.loads(tpath.read_text())
    else:
        set_source(source_id, status="transcribing", step="transcribing", progress=0.3)
        try:
            transcript = await groq.transcribe(audio, duration, wd, progress_cb(source_id, "transcribing", 0.3, 0.5))
        except groq.QuotaWait as e:
            raise Wait(e.until, str(e)) from e
        tpath.write_text(json.dumps(transcript, ensure_ascii=False))
        set_source(source_id, language=transcript.get("language", "")[:20])
    words = transcript["words"]
    if len(words) < 30:
        set_source(source_id, status="done", step="", progress=1, reason="Almost no speech found, so no clips.")
        return

    # 4. loudness
    lpath = wd / "loud.json"
    if lpath.exists():
        z = json.loads(lpath.read_text())
    else:
        set_source(source_id, status="scoring", step="loudness", progress=0.5)
        curve = await asyncio.to_thread(media.loudness, audio)
        z = moments.loud_z(curve)
        lpath.write_text(json.dumps([round(v, 2) for v in z]))

    # 5. moments (Gemini on the transcript)
    existing = db.all("SELECT * FROM clips WHERE source_id=?", (source_id,))
    if not existing:
        set_source(source_id, status="scoring", step="finding moments", progress=0.55)
        try:
            cands = await moments.pick(transcript, duration, st, src["title"])
        except ai.AIUnavailable as e:
            raise Wait(time.time() + 1800, f"Gemini busy or quota used up ({str(e)[:120]}); retrying in 30 min") from e
        now = db.now()
        for c in cands:
            c.loud = moments.loud_peak(z, c.start, c.end)
            db.insert("clips", {"source_id": source_id, "start": c.start, "end": c.end, "word_from": c.word_from,
                                "word_to": c.word_to, "llm_score": c.llm_score, "loud": round(c.loud, 2),
                                "score": round(moments.prescore(c), 1), "reason": c.reason[:300], "hook": c.hook[:120],
                                "caption": c.caption[:400], "hashtags": c.hashtags[:200], "status": "candidate",
                                "created_at": now, "updated_at": now})
        if not cands:
            set_source(source_id, status="done", step="", progress=1, reason="Gemini found no strong moments.")
            return

    # 6. music / laughter check, best first, until we have enough
    want = max(1, math.ceil(duration / 3600 * float(st["clips_per_hour"]) - 0.05))
    threshold = float(st["score_threshold"])
    rows = db.all("SELECT * FROM clips WHERE source_id=? ORDER BY score DESC", (source_id,))
    accepted = [r for r in rows if r["status"] not in ("candidate", "excluded")]
    accepted += [r for r in rows if r["status"] == "candidate" and r["music"]]
    for r in rows:
        if r["status"] != "candidate" or r["music"]:
            continue
        if len(accepted) >= want:
            db.update("clips", r["id"], {"status": "excluded", "note": "not in the top clips for this video",
                                         "updated_at": db.now()})
            continue
        if r["score"] + 12 < threshold:
            db.update("clips", r["id"], {"status": "excluded", "note": "score below the threshold", "updated_at": db.now()})
            continue
        set_source(source_id, status="scoring", step="checking music", progress=0.65)
        cut = wd / f"check_{r['id']}.mp3"
        await asyncio.to_thread(media.cut_audio, audio, r["start"], r["end"], cut)
        try:
            res = await moments.audio_check(cut.read_bytes())
        except ai.AIUnavailable as e:
            raise Wait(time.time() + 1800, f"Gemini busy or quota used up ({str(e)[:120]}); retrying in 30 min") from e
        finally:
            cut.unlink(missing_ok=True)
        c = moments.Candidate(r["word_from"], r["word_to"], r["start"], r["end"], r["llm_score"], "", "", "", "",
                              loud=r["loud"], laughter=bool(res["laughter"]), reaction=bool(res["reaction"]),
                              music=res["music"])
        score = round(moments.final_score(c), 1)
        upd = {"music": c.music, "laughter": int(c.laughter), "reaction": int(c.reaction), "score": score,
               "updated_at": db.now()}
        if not moments.music_ok(c.music, st["music_allowed"]):
            upd.update(status="excluded", note=f"music detected ({c.music}): {res.get('note', '')[:80]}")
        elif score < threshold:
            upd.update(status="excluded", note="score below the threshold")
        else:
            accepted.append(r)
        db.update("clips", r["id"], upd)

    # 7. previews
    todo = db.all("SELECT * FROM clips WHERE source_id=? AND status='candidate' AND music!='' ORDER BY score DESC",
                  (source_id,))
    ends = moments.boundaries(words, transcript["segments"])
    credit = credit_of(src)
    for i, r in enumerate(todo):
        set_source(source_id, status="rendering", step=f"preview {i + 1}/{len(todo)}",
                   progress=round(0.7 + 0.3 * i / max(1, len(todo)), 3))
        await make_preview(src, r, video, words, ends, credit)

    ready = db.one("SELECT COUNT(*) n FROM clips WHERE source_id=? AND status='review'", (source_id,))["n"]
    if ready:
        set_source(source_id, status="review", step="", progress=1, reason="")
        await push.send(f"🎬 {ready} clip{'s' if ready > 1 else ''} ready to review",
                        (src["title"] or src["url"])[:90], url=f"/#source/{source_id}", tag=f"src{source_id}")
    else:
        set_source(source_id, status="done", step="", progress=1,
                   reason="No moment passed the checks (music / score). See the excluded list.")


async def make_preview(src: dict, clip: dict, video: Path, words: list[dict], ends: set[int], credit: str):
    cd = clip_dir(src["id"], clip["id"])
    cd.mkdir(parents=True, exist_ok=True)
    plan_file = cd / "plan.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
    else:
        plan = await asyncio.to_thread(faces.plan, video, clip["start"], clip["end"])
        plan_file.write_text(json.dumps(plan))
    ass = captions.build(words[clip["word_from"]:clip["word_to"] + 1], clip["start"], clip["end"], hook=clip["hook"],
                         owner_text=clip["owner_text"], credit=credit, ends=ends, offset=clip["word_from"],
                         font=config.FONT_NAME, layout=plan["layout"])
    out = cd / "preview.mp4"
    try:
        await asyncio.to_thread(render.render, video, clip["start"], clip["end"], plan, ass, out, "preview", cd)
        await asyncio.to_thread(media.thumbnail, out, cd / "thumb.jpg", min(1.5, (clip["end"] - clip["start"]) / 2))
    except media.MediaError as e:
        db.update("clips", clip["id"], {"status": "excluded", "note": f"preview failed: {str(e)[:200]}",
                                        "updated_at": db.now()})
        return
    db.update("clips", clip["id"], {"status": "review", "layout": plan["layout"], "preview": str(out),
                                    "thumb": str(cd / "thumb.jpg"), "updated_at": db.now()})


# ---------------------------------------------------------------------------------------------------------
# Final render of an approved clip
# ---------------------------------------------------------------------------------------------------------
async def final(clip_id: int):
    clip = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not clip or clip["status"] not in ("approved", "rendering"):
        return
    src = db.one("SELECT * FROM sources WHERE id=?", (clip["source_id"],))
    video = Path(src["file"]) if src and src["file"] else None
    if not video or not video.exists():
        db.update("clips", clip_id, {"status": "review", "note": "The source video is gone, so the final can't be made.",
                                     "updated_at": db.now()})
        raise Refused("source video deleted")
    db.update("clips", clip_id, {"status": "rendering", "updated_at": db.now()})
    wd = work_dir(src["id"])
    transcript = json.loads((wd / "transcript.json").read_text())
    words = transcript["words"]
    ends = moments.boundaries(words, transcript["segments"])
    cd = clip_dir(src["id"], clip_id)
    plan_file = cd / "plan.json"
    plan = json.loads(plan_file.read_text()) if plan_file.exists() else await asyncio.to_thread(
        faces.plan, video, clip["start"], clip["end"])
    ass = captions.build(words[clip["word_from"]:clip["word_to"] + 1], clip["start"], clip["end"], hook=clip["hook"],
                         owner_text=clip["owner_text"], credit=credit_of(src), ends=ends, offset=clip["word_from"],
                         font=config.FONT_NAME, layout=plan["layout"])
    config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    out = final_path(clip_id)
    try:
        await asyncio.to_thread(render.render, video, clip["start"], clip["end"], plan, ass, out, "final", cd)
    except media.MediaError as e:
        db.update("clips", clip_id, {"status": "approved", "note": f"final render failed: {str(e)[:200]}",
                                     "updated_at": db.now()})
        raise
    db.update("clips", clip_id, {"status": "ready", "final": str(out), "final_size": out.stat().st_size, "note": "",
                                 "updated_at": db.now()})
    publish.queue_for(clip_id)
    await push.send("✅ Clip ready to post", (clip["hook"] or src["title"])[:90], url=f"/#clip/{clip_id}",
                    tag=f"clip{clip_id}")


# ---------------------------------------------------------------------------------------------------------
# Record a live stream, then hand it to `process`
# ---------------------------------------------------------------------------------------------------------
async def record(source_id: int, stop: asyncio.Event | None = None):
    src = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not src:
        return
    disk_guard()
    st = db.settings()
    wd = work_dir(source_id)
    set_source(source_id, status="recording", step="recording live", progress=0)
    try:
        f = await fetch.record(src["url"], wd, float(st["max_source_hours"]) * 3600, src["platform"], stop)
    except fetch.FetchError as e:
        set_source(source_id, status="failed", reason=f"Recording failed: {e}")
        db.event(f"Recording failed for “{src['title'][:60]}”: {e}", "record", "error", source_id)
        raise Refused(str(e)) from e
    set_source(source_id, file=str(f), size=f.stat().st_size, status="queued", step="", progress=0)
    db.event(f"Recorded “{src['title'][:60]}” ({f.stat().st_size / 1e9:.1f} GB)", "record", "info", source_id)
    db.enqueue("process", source_id=source_id, priority=10)


# ---------------------------------------------------------------------------------------------------------
# Cleanup (owner's rules: delete sources when every clip is decided or after 7 days; clips 30 days after done)
# ---------------------------------------------------------------------------------------------------------
def cleanup():
    now = db.now()
    for src in db.all("SELECT * FROM sources WHERE files_deleted=0 AND status IN "
                      "('review','done','failed','rejected_by_gate')"):
        open_clips = db.one("SELECT COUNT(*) n FROM clips WHERE source_id=? AND status IN "
                            "('review','approved','rendering')", (src["id"],))["n"]
        old = now - src["created_at"] > config.KEEP_SOURCE_DAYS * 86400
        if open_clips and not old:
            continue
        if old:
            db.execute("UPDATE clips SET status='expired', updated_at=? WHERE source_id=? AND status IN "
                       "('review','approved')", (now, src["id"]))
        wd = work_dir(src["id"])
        for f in wd.iterdir() if wd.exists() else []:
            if f.is_file() and f.name not in ("transcript.json", "loud.json"):
                f.unlink(missing_ok=True)
        clips_root = wd / "clips"
        if clips_root.exists():
            shutil.rmtree(clips_root, ignore_errors=True)
        db.update("sources", src["id"], {"files_deleted": 1, "file": "",
                                         **({"status": "done"} if src["status"] == "review" else {})})
        db.execute("UPDATE clips SET preview='', thumb='' WHERE source_id=?", (src["id"],))
    for c in db.all("SELECT * FROM clips WHERE final!='' AND status IN ('posted','rejected','expired') AND "
                    "updated_at < ?", (now - config.KEEP_CLIP_DAYS * 86400,)):
        Path(c["final"]).unlink(missing_ok=True)
        db.update("clips", c["id"], {"final": ""})
