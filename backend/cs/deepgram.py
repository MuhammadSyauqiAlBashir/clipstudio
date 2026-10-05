"""Deepgram (backup transcription): used only for a chunk Groq can't take right now (its free daily allowance is
used up or it asks for a long wait). Paid from the one-time $200 sign-up credit (no card on the account), so when
the credit runs out Deepgram refuses and Clip Studio simply waits for Groq again. Output = Groq's format."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("cs.deepgram")

URL = "https://api.deepgram.com/v1/listen"
API = "https://api.deepgram.com/v1"
# Deepgram language codes → the names Whisper/Groq use (the rest of the app expects these)
LANG_NAMES = {"id": "Indonesian", "en": "English", "ms": "Malay", "jv": "Javanese", "su": "Sundanese",
              "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "es": "Spanish", "fr": "French", "de": "German",
              "pt": "Portuguese", "hi": "Hindi", "ar": "Arabic", "th": "Thai", "vi": "Vietnamese", "tl": "Tagalog"}
LANG_CODES = {v.lower(): k for k, v in LANG_NAMES.items()}


class Unavailable(Exception):
    """Deepgram can't be used now (no key, credit used up, key refused, not answering)."""


def configured() -> bool:
    return bool(config.DEEPGRAM_API_KEY)


def usable() -> bool:
    return configured() and (db.kv_get("deepgram_off", {}) or {}).get("until", 0) < time.time()


def _off(why: str, hours: float):
    db.kv_set("deepgram_off", {"until": time.time() + hours * 3600, "why": why[:200]})
    db.event(f"Deepgram backup paused for {hours:g} h: {why[:200]}", "transcribe", "warn")


def to_result(d: dict, offset: float) -> dict:
    """Deepgram's answer → {language, words, segments} like groq._clean (times shifted by `offset`)."""
    ch = ((d.get("results") or {}).get("channels") or [{}])[0]
    alt = (ch.get("alternatives") or [{}])[0]
    words = []
    for w in alt.get("words") or []:
        text = str(w.get("punctuated_word") or w.get("word") or "").strip()
        if text:
            words.append({"word": text, "start": round(float(w["start"]) + offset, 3),
                          "end": round(float(w["end"]) + offset, 3)})
    segs = []
    for p in ((alt.get("paragraphs") or {}).get("paragraphs") or []):
        for s in p.get("sentences") or []:
            segs.append({"text": str(s.get("text", "")).strip(), "start": round(float(s["start"]) + offset, 3),
                         "end": round(float(s["end"]) + offset, 3)})
    if not segs and words:
        segs = [{"text": " ".join(w["word"] for w in words), "start": words[0]["start"], "end": words[-1]["end"]}]
    code = str(ch.get("detected_language") or "").split("-")[0].lower()
    return {"language": LANG_NAMES.get(code, code), "words": words, "segments": segs}


async def transcribe(path: Path, offset: float, language: str = "") -> dict:
    """One chunk (≤ 10 min mp3). `language` = what earlier chunks found (e.g. "Indonesian"), else auto-detect."""
    if not usable():
        raise Unavailable("Deepgram isn't set up or is paused")
    audio = path.read_bytes()
    code = LANG_CODES.get((language or "").lower(), "")
    params = {"model": config.DEEPGRAM_MODEL, "smart_format": "true", "punctuate": "true", "paragraphs": "true"}
    if code:
        params["language"] = code
    else:
        params["detect_language"] = "true"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as http:
        for attempt in range(3):
            try:
                r = await http.post(URL, params=params, content=audio, headers={
                    "Authorization": f"Token {config.DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"})
            except httpx.HTTPError as e:
                log.warning("deepgram network error: %s", e)
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                res = to_result(r.json(), offset)
                if code and not res["language"]:
                    res["language"] = language
                return res
            if r.status_code == 400 and params["model"] != config.DEEPGRAM_FALLBACK_MODEL:
                # e.g. this language isn't offered on the newest model: the older model covers more languages
                log.info("deepgram %s refused (%s), trying %s", params["model"], r.text[:200], config.DEEPGRAM_FALLBACK_MODEL)
                params["model"] = config.DEEPGRAM_FALLBACK_MODEL
                continue
            if r.status_code in (401, 402, 403):
                _off(f"Deepgram refused the key or the credit is used up (HTTP {r.status_code})", 6)
                raise Unavailable(f"Deepgram HTTP {r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(10 * (attempt + 1))
                continue
            raise Unavailable(f"Deepgram HTTP {r.status_code}: {r.text[:200]}")
    _off("Deepgram isn't answering", 0.5)
    raise Unavailable("Deepgram isn't answering")


async def balance() -> dict:
    """Credit left, cached for an hour: {"amount": 187.4, "units": "usd", "at": ts} or {} if the key can't read it."""
    cached = db.kv_get("deepgram_balance", {}) or {}
    if not configured() or cached.get("at", 0) > time.time() - 3600:
        return cached
    out = {"at": time.time()}
    try:
        async with httpx.AsyncClient(timeout=8) as http:  # the More page waits for this once an hour
            h = {"Authorization": f"Token {config.DEEPGRAM_API_KEY}"}
            projects = (await http.get(f"{API}/projects", headers=h)).json().get("projects") or []
            if projects:
                r = await http.get(f"{API}/projects/{projects[0]['project_id']}/balances", headers=h)
                bal = (r.json().get("balances") or []) if r.status_code == 200 else []
                if bal:
                    out.update(amount=round(sum(float(b.get("amount") or 0) for b in bal), 2),
                               units=bal[0].get("units", "usd"))
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.info("deepgram balance: %s", e)
    db.kv_set("deepgram_balance", out)
    return out
