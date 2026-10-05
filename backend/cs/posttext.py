"""The post caption for each platform (shown in the app and used by auto-posting)."""

from __future__ import annotations

import re

from . import db

PLATFORM_TAGS = {"tiktok": "", "youtube": " #shorts", "instagram": " #reels", "facebook": " #reels"}


def post_caption(c: dict, src: dict, platform: str = "tiktok") -> str:
    st = db.settings()
    from .campaigns import hashtags_for_source  # required campaign hashtags go first and are never dropped
    required = " ".join(hashtags_for_source(src))
    tags = " ".join(dict.fromkeys((required + " " + c["hashtags"] + " " + st["hashtags"] + PLATFORM_TAGS.get(platform, "")).split()))
    indonesian = (src.get("language") or "").lower().startswith(("indonesian", "id"))
    creator = src.get("creator") or ("kreator aslinya" if indonesian else "the original creator")
    m = re.search(r"youtube\.com/@([\w.\-]+)|(?:twitch\.tv|kick\.com)/(\w+)", src.get("creator_url") or "")
    if m:
        creator = f"@{m.group(1) or m.group(2)}"
    text = st["caption_template"].format(hook=c["hook"], caption=c["caption"], creator=creator,
                                         source_url=src.get("url") or src.get("creator_url") or "", hashtags=tags)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
