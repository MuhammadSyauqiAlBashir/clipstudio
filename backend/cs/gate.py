"""The source gate (owner's hard rules), checked before anything is downloaded:
permission must not be blocked, no movie/TV/studio/sports titles, sensible length."""

from __future__ import annotations

import re

PERMISSIONS = ("explicit", "platform-default", "campaign", "blocked")


def blocked_keyword(title: str, blocklist: str) -> str:
    t = f" {title.lower()} "
    for kw in (k.strip().lower() for k in blocklist.split(",")):
        if kw and re.search(rf"(?<![\w]){re.escape(kw)}(?![\w])", t):
            return kw
    return ""


def check(*, permission: str, title: str, duration: float, live: bool, settings: dict,
          min_minutes: float = 0) -> str:
    """'' when the source may be clipped, else the reason it was refused."""
    if permission not in PERMISSIONS or permission == "blocked":
        return "Permission is 'blocked': set explicit / platform-default / campaign first."
    kw = blocked_keyword(title, settings.get("keyword_blocklist", ""))
    if kw:
        return f"Title matches the blocklist ('{kw}'): movies/TV/studio/sports are never clipped."
    if not live:
        if duration and duration < 60:
            return "Too short to clip (under 1 minute)."
        if min_minutes and duration and duration < min_minutes * 60:
            return f"Shorter than this channel's minimum ({min_minutes:g} min)."
    return ""
