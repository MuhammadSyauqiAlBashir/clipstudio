"""Groq Whisper (free tier): word-timestamped transcripts in 10-minute chunks with a 2 s overlap.
Each chunk's result is cached in the source's work dir, so a job paused by the quota resumes where it stopped."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path

import httpx

from . import config, db, deepgram, media

log = logging.getLogger("cs.groq")

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CHUNK = 600.0
OVERLAP = 2.0


class QuotaWait(Exception):
    """Free-tier limit reached: try again at `until` (unix time)."""

    def __init__(self, until: float, why: str):
        super().__init__(why)
        self.until = until


def chunk_plan(duration: float) -> list[tuple[float, float]]:
    if duration <= CHUNK + 30:
        return [(0.0, duration)]
    out, s = [], 0.0
    while s < duration:
        e = min(duration, s + CHUNK)
        out.append((s, e))
        if e >= duration:
            break
        s = e - OVERLAP
    return out


def merge(chunks: list[tuple[float, float, dict]]) -> dict:
    """Join chunk results (times already absolute): each word belongs to the chunk whose middle of the overlap it
    falls on the right side of."""
    words, segments, langs = [], [], []
    n = len(chunks)
    for i, (s, e, res) in enumerate(chunks):
        lo = s + OVERLAP / 2 if i > 0 else -1
        hi = e - OVERLAP / 2 if i < n - 1 else math.inf
        for w in res.get("words", []):
            if lo <= w["start"] < hi:
                words.append(w)
        for seg in res.get("segments", []):
            mid = (seg["start"] + seg["end"]) / 2
            if lo <= mid < hi:
                segments.append(seg)
        if res.get("language"):
            langs.append(res["language"])
    words.sort(key=lambda w: w["start"])
    segments.sort(key=lambda g: g["start"])
    lang = max(set(langs), key=langs.count) if langs else ""
    return {"language": lang, "words": words, "segments": segments}


def _clean(res: dict, offset: float) -> dict:
    words = [{"word": str(w.get("word", "")).strip(), "start": round(float(w["start"]) + offset, 3),
              "end": round(float(w["end"]) + offset, 3)}
             for w in res.get("words") or [] if str(w.get("word", "")).strip()]
    segs = [{"text": str(g.get("text", "")).strip(), "start": round(float(g["start"]) + offset, 3),
             "end": round(float(g["end"]) + offset, 3)} for g in res.get("segments") or []]
    return {"language": res.get("language", ""), "words": words, "segments": segs}


async def _request(path: Path) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as http:
        audio = path.read_bytes()  # ≤ ~4 MB per 10-minute chunk
        data = {"model": config.GROQ_MODEL, "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"]}
        for attempt in range(4):
            try:
                r = await http.post(URL, headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}, data=data,
                                    files={"file": (path.name, audio, "audio/mpeg")})
            except httpx.HTTPError as e:
                log.warning("groq network error: %s", e)
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 60)
                if wait > 120:
                    raise QuotaWait(time.time() + wait + 5, f"Groq limit reached, waiting {int(wait // 60)} min")
                await asyncio.sleep(wait + 1)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
    raise QuotaWait(time.time() + 900, "Groq not answering, retrying in 15 min")


async def transcribe(audio: Path, duration: float, work: Path, progress=None) -> dict:
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    plan = chunk_plan(duration)
    results = []
    for i, (s, e) in enumerate(plan):
        cache = work / f"groq_{i:03d}.json"
        if cache.exists():
            results.append((s, e, json.loads(cache.read_text())))
            continue
        need = e - s
        part = work / f"chunk_{i:03d}.mp3"
        await asyncio.to_thread(media.ffmpeg, "-ss", f"{s:.2f}", "-t", f"{need:.2f}", "-i", str(audio), "-c", "copy",
                                str(part))
        try:
            used = db.usage_get("groq_seconds")
            if used + need > config.GROQ_DAILY_SECONDS:
                raise QuotaWait(time.time() + 3600, f"Groq daily audio budget used ({int(used)} s today)")
            raw = await _request(part)
            db.usage_add("groq_seconds", max(10.0, need))  # Groq bills at least 10 s per request
            res = _clean(raw, s)
        except QuotaWait as wait:
            # Groq can't take it now: the backup (Deepgram credit) does this chunk, if it's set up and has credit
            if not deepgram.usable():
                raise
            known = [r[2].get("language") for r in results if r[2].get("language")]
            try:
                res = await deepgram.transcribe(part, s, known[-1] if known else "")
            except deepgram.Unavailable as e:
                log.info("deepgram backup unavailable: %s", e)
                raise wait from e
            db.usage_add("deepgram_seconds", need)
            res["by"] = "deepgram"
            log.info("chunk %d transcribed by Deepgram (%s)", i, wait)
        finally:
            part.unlink(missing_ok=True)
        cache.write_text(json.dumps(res, ensure_ascii=False))
        results.append((s, e, res))
        if progress:
            progress((i + 1) / len(plan))
    return merge(results)
