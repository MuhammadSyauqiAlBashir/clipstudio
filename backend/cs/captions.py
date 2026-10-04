"""Burned-in captions as one .ass (libass) file: karaoke highlight (3-5 words on screen, the spoken word lit),
a hook title for the first seconds, the creator credit, and the owner's own text.
PlayRes is 1080x1920; libass scales it for the 540x960 preview."""

from __future__ import annotations

import re

HIGHLIGHT = "&H0000E5FF&"  # yellow (ASS colours are &HAABBGGRR)
MAX_WORDS = 4
MAX_CHARS = 22
HOOK_SECONDS = 3.2

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,7,3,2,70,70,600,1
Style: Hook,{font},74,&H00FFFFFF,&H00FFFFFF,&H00101010,&H00101010,0,0,0,0,100,100,0,0,3,18,0,8,90,90,250,1
Style: Owner,{font},62,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,3,14,0,8,90,90,420,1
Style: Credit,{font},38,&H40FFFFFF,&H40FFFFFF,&H80000000,&H80000000,0,0,0,0,100,100,1,0,1,3,0,7,48,48,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(text: str) -> str:
    """Text safe for an ASS event (no override blocks, no line-break codes)."""
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def caption_word(w: str) -> str:
    return clean(w).strip(" ,;:.").upper()


def wrap(text: str, width: int) -> str:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return r"\N".join(out)


def chunks(words: list[dict], ends: set[int] | None = None, offset: int = 0) -> list[list[dict]]:
    """Group words for the screen: at most MAX_WORDS / MAX_CHARS, breaking at sentence ends and pauses."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        text = caption_word(w["word"])
        if not text:
            continue
        chars = sum(len(caption_word(x["word"])) + 1 for x in cur) + len(text)
        gap = w["start"] - cur[-1]["end"] if cur else 0
        if cur and (len(cur) >= MAX_WORDS or chars > MAX_CHARS or gap > 0.6):
            out.append(cur)
            cur = []
        cur.append(w)
        if ends and (offset + i) in ends:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def build(words: list[dict], clip_start: float, clip_end: float, *, hook: str = "", owner_text: str = "",
          credit: str = "", ends: set[int] | None = None, offset: int = 0, font: str = "Anton",
          layout: str = "") -> str:
    """`words` are the clip's words with absolute times; `offset` is the index of words[0] in the transcript."""
    dur = clip_end - clip_start
    lines = [HEADER.replace("{font}", font)]
    groups = chunks(words, ends, offset)
    pos = r"{\an5}" if layout == "split" else ""  # split screen: captions on the seam between the two speakers
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else clip_end
        group_end = min(next_start, group[-1]["end"] + 0.6)
        texts = [caption_word(w["word"]) for w in group]
        for wi, w in enumerate(group):
            a = w["start"] - clip_start
            b = (group[wi + 1]["start"] if wi + 1 < len(group) else group_end) - clip_start
            if b <= 0 or a >= dur:
                continue
            parts = [(f"{{\\c{HIGHLIGHT}\\fscx112\\fscy112}}{t}{{\\r}}" if k == wi else t) for k, t in enumerate(texts)]
            lines.append(f"Dialogue: 1,{ts(max(0.0, a))},{ts(min(dur, b))},Caption,,0,0,0,,{pos}{' '.join(parts)}")
    if hook:
        lines.append(f"Dialogue: 2,{ts(0)},{ts(min(dur, HOOK_SECONDS))},Hook,,0,0,0,,{wrap(clean(hook).upper(), 20)}")
    if owner_text:
        start = 0 if not hook else min(dur, HOOK_SECONDS)
        lines.append(f"Dialogue: 2,{ts(start)},{ts(dur)},Owner,,0,0,0,,{wrap(clean(owner_text), 26)}")
    if credit:
        lines.append(f"Dialogue: 3,{ts(0)},{ts(dur)},Credit,,0,0,0,,{clean(credit)}")
    return "\n".join(lines) + "\n"
