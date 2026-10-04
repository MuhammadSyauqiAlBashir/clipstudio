"""Finding the best moments: Gemini reads the transcript and returns word-index ranges (cuts land on words),
cuts are snapped to sentence ends/pauses, loudness spikes and laughter add points, music excludes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import ai

PAD_BEFORE = 0.15
PAD_AFTER = 0.35
PAUSE = 0.35
SENTENCE_END = (".", "!", "?", "…", "。")


@dataclass
class Candidate:
    word_from: int
    word_to: int
    start: float
    end: float
    llm_score: float
    reason: str
    hook: str
    caption: str
    hashtags: str
    loud: float = 0.0
    laughter: bool = False
    reaction: bool = False
    music: str = ""
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


# ---- sentence boundaries -------------------------------------------------------------------------------
def boundaries(words: list[dict], segments: list[dict]) -> set[int]:
    """Indices of words that end a thought: a pause after them, punctuation, or the end of a punctuated segment."""
    ends: set[int] = set()
    n = len(words)
    for i, w in enumerate(words):
        if i == n - 1 or words[i + 1]["start"] - w["end"] >= PAUSE or w["word"].rstrip('"\')').endswith(SENTENCE_END):
            ends.add(i)
    j = 0
    for seg in segments:
        if not seg["text"].rstrip('"\')').endswith(SENTENCE_END):
            continue
        while j < n and words[j]["end"] <= seg["end"] + 0.25:
            j += 1
        if j > 0:
            ends.add(j - 1)
    return ends


def snap(a: int, b: int, words: list[dict], ends: set[int], min_s: float, max_s: float) -> tuple[int, int] | None:
    """Move the cut so it starts at the beginning of a sentence and ends at the end of one, within the length range."""
    n = len(words)
    a, b = max(0, min(a, n - 1)), max(0, min(b, n - 1))
    if b < a:
        a, b = b, a
    # start: back to just after the previous boundary (at most 8 s back)
    s = a
    while s > 0 and (s - 1) not in ends and words[a]["start"] - words[s - 1]["start"] <= 8:
        s -= 1
    if s > 0 and (s - 1) not in ends:
        s = a
    # end: forward to the next boundary (at most 8 s on)
    e = b
    while e < n - 1 and e not in ends and words[e + 1]["end"] - words[b]["end"] <= 8:
        e += 1

    def dur(x: int, y: int) -> float:
        return words[y]["end"] - words[x]["start"] + PAD_BEFORE + PAD_AFTER

    # too short: extend to later boundaries
    while dur(s, e) < min_s and e < n - 1:
        e += 1
        while e < n - 1 and e not in ends:
            e += 1
    # too long: back to the latest boundary that fits, else a hard cut at a word
    if dur(s, e) > max_s:
        fits = [i for i in range(s, e + 1) if i in ends and min_s <= dur(s, i) <= max_s]
        if fits:
            e = fits[-1]
        else:
            while e > s and dur(s, e) > max_s:
                e -= 1
    if dur(s, e) < min_s * 0.8:
        return None
    return s, e


def times(s: int, e: int, words: list[dict], duration: float) -> tuple[float, float]:
    return max(0.0, words[s]["start"] - PAD_BEFORE), min(duration, words[e]["end"] + PAD_AFTER)


# ---- loudness ------------------------------------------------------------------------------------------
def loud_z(curve: list[float], step: float = 0.5, window: float = 60.0) -> list[float]:
    """z-score of each loudness point against the preceding `window` seconds."""
    k = max(4, int(window / step))
    out = []
    s1 = s2 = 0.0
    q: list[float] = []
    for v in curve:
        if len(q) >= 8:
            mean = s1 / len(q)
            var = max(1e-6, s2 / len(q) - mean * mean)
            out.append((v - mean) / math.sqrt(var))
        else:
            out.append(0.0)
        q.append(v)
        s1 += v
        s2 += v * v
        if len(q) > k:
            old = q.pop(0)
            s1 -= old
            s2 -= old * old
    return out


def loud_peak(z: list[float], start: float, end: float, step: float = 0.5) -> float:
    lo, hi = int(start / step), int(end / step) + 1
    vals = z[lo:hi]
    return max(vals) if vals else 0.0


# ---- scoring -------------------------------------------------------------------------------------------
MUSIC_ORDER = ["none", "faint", "clear", "song"]


def music_ok(level: str, allowed: str) -> bool:
    if level not in MUSIC_ORDER:
        return False  # unchecked counts as not cleared (hard rule: music excluded)
    return MUSIC_ORDER.index(level) <= MUSIC_ORDER.index(allowed if allowed in MUSIC_ORDER else "none")


def loud_boost(z: float) -> float:
    return max(0.0, min(12.0, (z - 1.5) * 4))


def prescore(c: Candidate) -> float:
    return c.llm_score * 8 + loud_boost(c.loud)


def final_score(c: Candidate) -> float:
    return prescore(c) + (8 if c.laughter else 0) + (4 if c.reaction else 0)


def drop_overlaps(cands: list[Candidate], key=prescore, max_overlap: float = 0.3) -> list[Candidate]:
    kept: list[Candidate] = []
    for c in sorted(cands, key=key, reverse=True):
        clash = False
        for k in kept:
            ov = min(c.end, k.end) - max(c.start, k.start)
            if ov > 0 and ov / min(c.duration, k.duration) > max_overlap:
                clash = True
                break
        if not clash:
            kept.append(c)
    return kept


# ---- Gemini ----------------------------------------------------------------------------------------------
SYSTEM = """You are an expert short-form video editor who finds the moments in long videos that work as standalone
vertical clips (TikTok, YouTube Shorts, Instagram Reels).
Good moments: a punchline, a surprising statement, a strong opinion, a question followed by its payoff, an emotional
beat, a funny exchange, a useful insight told well. Each one must be a complete thought: a clear setup and a payoff,
understandable without the rest of the video.
Avoid: sponsor reads and ads, greetings and outros, people singing or music playing, reading chat donations, filler,
anything that needs earlier context.
The transcript lines are "[first-last] (mm:ss) text" where first/last are word indices. Return word indices from
those ranges (start at a line's first index, end at a line's last index)."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {"moments": {"type": "ARRAY", "items": ai.obj({
        "start_word": {"type": "INTEGER"},
        "end_word": {"type": "INTEGER"},
        "score": {"type": "INTEGER", "description": "1-10: how likely to go viral as a standalone clip"},
        "reason": {"type": "STRING", "description": "one short sentence: why it works"},
        "hook": {"type": "STRING", "description": "on-screen hook title, max 7 words, same language as the speech, "
                                                  "honest (no fake claims), no emojis, no hashtags"},
        "caption": {"type": "STRING", "description": "one-sentence post caption in the same language"},
        "hashtags": {"type": "STRING", "description": "3-4 relevant hashtags separated by spaces, each starting with #"},
    })}},
    "required": ["moments"], "propertyOrdering": ["moments"],
}


def transcript_lines(words: list[dict], segments: list[dict], lo: int, hi: int) -> list[str]:
    """Lines of the transcript between word indices lo..hi, one per segment."""
    lines = []
    j = lo
    for seg in segments:
        if seg["end"] < words[lo]["start"] or seg["start"] > words[hi]["end"]:
            continue
        a = j
        while j <= hi and words[j]["end"] <= seg["end"] + 0.25:
            j += 1
        if j > a:
            t = int(words[a]["start"])
            text = " ".join(w["word"] for w in words[a:j])
            lines.append(f"[{a}-{j - 1}] ({t // 60:02d}:{t % 60:02d}) {text}")
    if j <= hi:  # words after the last segment
        t = int(words[j]["start"])
        lines.append(f"[{j}-{hi}] ({t // 60:02d}:{t % 60:02d}) " + " ".join(w["word"] for w in words[j:hi + 1]))
    return lines


def windows(words: list[dict], minutes: float = 12, overlap: float = 1) -> list[tuple[int, int]]:
    if not words:
        return []
    out, lo = [], 0
    n = len(words)
    while lo < n:
        t_end = words[lo]["start"] + minutes * 60
        hi = lo
        while hi < n - 1 and words[hi + 1]["start"] < t_end:
            hi += 1
        out.append((lo, hi))
        if hi >= n - 1:
            break
        back = words[hi]["start"] - overlap * 60
        nlo = hi
        while nlo > lo and words[nlo - 1]["start"] > back:
            nlo -= 1
        lo = max(lo + 1, nlo)
    return out


async def pick(transcript: dict, duration: float, settings: dict, title: str = "") -> list[Candidate]:
    words, segments = transcript["words"], transcript["segments"]
    if len(words) < 20:
        return []
    ends = boundaries(words, segments)
    min_s, max_s = float(settings["min_clip_seconds"]), float(settings["max_clip_seconds"])
    out: list[Candidate] = []
    for lo, hi in windows(words):
        span_min = (words[hi]["end"] - words[lo]["start"]) / 60
        want = max(2, math.ceil(span_min / 60 * float(settings["clips_per_hour"]) * 2))
        prompt = (f"Video title: {title or '(unknown)'}\nLanguage: {transcript.get('language') or 'auto'}\n"
                  f"Find up to {want} moments, each {int(min_s)}-{int(max_s)} seconds long. Fewer is fine if the "
                  f"material is weak; return an empty list if nothing is good.\n\nTranscript:\n"
                  + "\n".join(transcript_lines(words, segments, lo, hi)))
        res = await ai.generate([prompt], system=SYSTEM, schema=SCHEMA, smart=True)
        for m in res.get("moments", []):
            a, b = int(m["start_word"]), int(m["end_word"])
            if not (lo <= a <= hi and lo <= b <= hi + 200):
                continue
            snapped = snap(a, b, words, ends, min_s, max_s)
            if not snapped:
                continue
            s, e = snapped
            start, end = times(s, e, words, duration)
            out.append(Candidate(s, e, start, end, float(max(1, min(10, int(m["score"])))), m["reason"].strip(),
                                 m["hook"].strip(), m["caption"].strip(), m["hashtags"].strip()))
    return drop_overlaps(out)


AUDIO_SCHEMA = ai.obj({
    "music": {"type": "STRING", "enum": ["none", "faint", "clear", "song"],
              "description": "none = no music at all; faint = quiet background music; clear = clearly audible "
                             "music; song = a recognisable song or someone singing one"},
    "laughter": {"type": "BOOLEAN", "description": "audible laughter"},
    "reaction": {"type": "BOOLEAN", "description": "a strong reaction: shouting, cheering, gasps, crowd noise"},
    "note": {"type": "STRING", "description": "max 12 words"},
})


async def audio_check(mp3: bytes) -> dict:
    return await ai.generate([ai.audio_part(mp3), "Listen to this audio clip and report what you hear."],
                             schema=AUDIO_SCHEMA)
