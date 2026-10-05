"""Clipping campaigns (public catalogue only): Clippo's public campaign list is read a few times a day and kept in
Clip Studio's DB (campaigns that disappear are marked ended, never deleted). Joining and submitting happen in the
platform's own app by the owner; Clip Studio imports the footage, adds the required hashtags to captions and lists
the posted links to submit."""

from __future__ import annotations

import html
import logging
import re
import time

import httpx

from . import db, push

log = logging.getLogger("cs.campaigns")

CLIPPO_LIST = "https://api.clippo.id/v1/campaigns"
CLIPPO_PAGE = "https://app.clippo.id/campaigns/{id}"
CLIPPO_PLATFORMS = {0: "tiktok", 1: "instagram", 2: "facebook"}
MY_PLATFORMS = {"tiktok", "instagram", "youtube", "facebook"}


def plain(html_text: str) -> str:
    """The campaign brief as plain text with line breaks (it's untrusted HTML: never rendered as HTML)."""
    t = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", html_text or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in t.splitlines())).strip()


def flags_for(c: dict) -> list[str]:
    """Warnings that matter for an automatic pipeline (owner's music rule, edits Clip Studio can't do)."""
    text = f"{c.get('title', '')}\n{c.get('brief', '')}".lower()
    out = []
    if re.search(r"\b(audio|lagu|sound|musik|music|ost)\b", text):
        out.append("needs_audio")  # conflicts with the 'music excluded' rule
    if re.search(r"(di ?akhir|diakhir|wajib ada|wajib bahas|wajib mention|wajib sebut|wajib tampil|teks \")", text):
        out.append("custom_edit")
    if c.get("budget_used", 0) >= 90:
        out.append("budget_low")
    if not set(c.get("platforms") or []) & MY_PLATFORMS:
        out.append("other_platforms")
    if c.get("product_cart"):
        out.append("product_cart")
    return out


def normalize_clippo(raw: dict) -> dict:
    footage = [{"url": a.get("url", ""), "required": bool(a.get("isRequired")), "text": (a.get("displayText") or "")[:200]}
               for a in raw.get("assetVideoUrlsV2") or [] if a.get("url")]
    if not footage:
        footage = [{"url": u, "required": False, "text": ""} for u in raw.get("assetVideoUrls") or []]
    c = {
        "platform": "clippo", "ext_id": raw["id"], "title": (raw.get("title") or "")[:200],
        "creator": ((raw.get("creator") or {}).get("user") or {}).get("name", "")[:120],
        "rate": int(raw.get("ratePerKView") or 0), "rate_unit": int(raw.get("rateUnit") or 1000),
        "platforms": [CLIPPO_PLATFORMS.get(p, f"other{p}") for p in raw.get("platformSupported") or []],
        "budget_used": float(raw.get("budgetPercentage") or 0), "clippers": int(raw.get("clippersJoinedCount") or 0),
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in (raw.get("hashtags") or []) if h][:20],
        "footage": footage[:30], "brief": plain(raw.get("requirementsV2") or raw.get("requirements") or "")[:6000],
        "image": raw.get("imageUrl") or "", "url": CLIPPO_PAGE.format(id=raw["id"]),
        "product_cart": bool(raw.get("isYellowCart")), "max_reward": int(raw.get("maxRewardPerBatch") or 0),
        "start": raw.get("startDate") or "", "end": raw.get("endDate") or "",
    }
    c["flags"] = flags_for(c)
    return c


async def fetch_clippo() -> list[dict]:
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as http:
        for page in range(1, 21):
            r = await http.get(CLIPPO_LIST, params={"page": page, "limit": 50})
            r.raise_for_status()
            d = (r.json() or {}).get("data") or {}
            out += [normalize_clippo(x) for x in d.get("data") or [] if x.get("id")]
            if page >= int(d.get("totalPage") or 1):
                break
    return out


def upsert(items: list[dict], platform: str) -> list[dict]:
    """Store the catalogue; returns the campaigns seen for the first time. Missing ones become 'ended' (kept)."""
    now = db.now()
    new = []
    seen = set()
    for c in items:
        seen.add(c["ext_id"])
        row = db.one("SELECT id FROM campaigns WHERE platform=? AND ext_id=?", (platform, c["ext_id"]))
        data = {"title": c["title"], "creator": c["creator"], "rate": c["rate"], "platforms": db.jdump(c["platforms"]),
                "budget_used": c["budget_used"], "clippers": c["clippers"], "hashtags": db.jdump(c["hashtags"]),
                "footage": db.jdump(c["footage"]), "brief": c["brief"], "image": c["image"], "url": c["url"],
                "flags": db.jdump(c["flags"]), "info": db.jdump({k: c[k] for k in ("rate_unit", "max_reward", "start",
                                                                                     "end", "product_cart")}),
                "status": "open", "last_seen": now}
        if row:
            db.update("campaigns", row["id"], data)
        else:
            db.insert("campaigns", {"platform": platform, "ext_id": c["ext_id"], "first_seen": now, **data})
            new.append(c)
    for r in db.all("SELECT id, ext_id FROM campaigns WHERE platform=? AND status='open'", (platform,)):
        if r["ext_id"] not in seen:
            db.update("campaigns", r["id"], {"status": "ended"})
    return new


async def sync_joined():
    """Mark the campaigns the owner has joined on Clippo (from his own account)."""
    from . import clippo
    if not clippo.configured():
        return
    ids = await clippo.joined_ids()
    for r in db.all("SELECT id, ext_id, joined FROM campaigns WHERE platform='clippo'"):
        if (r["ext_id"] in ids) and not r["joined"]:
            db.update("campaigns", r["id"], {"joined": 1})


async def auto_submit() -> int:
    """Check posted clips of joined Clippo campaigns with Clippo itself and submit the eligible ones.
    Not-yet-eligible links (e.g. under 1,000 views) are checked again next time, for up to 14 days."""
    from . import clippo, push
    if not clippo.configured() or not db.settings().get("auto_submit", True):
        return 0
    sent = 0
    for camp in db.all("SELECT * FROM campaigns WHERE platform='clippo' AND joined=1 AND status='open'"):
        allowed = set(db.jload(camp["platforms"], []))
        pending: list[tuple[int, str, str]] = []
        for c in db.all("SELECT c.id, c.posted, c.submitted, c.updated_at FROM clips c JOIN sources s ON s.id=c.source_id "
                        "WHERE s.campaign_id=? AND c.status IN ('ready','posted')", (camp["id"],)):
            posted, sub = db.jload(c["posted"], {}) or {}, db.jload(c["submitted"], {}) or {}
            for platform, url in posted.items():
                if platform in allowed and str(url).startswith("http") and platform not in sub:
                    post = db.one("SELECT posted_at FROM posts WHERE clip_id=? AND platform=?", (c["id"], platform))
                    when = (post or {}).get("posted_at") or c["updated_at"]
                    if time.time() - when < 14 * 86400:
                        pending.append((c["id"], platform, url))
        if not pending:
            continue
        results = await clippo.bulk_check(camp["ext_id"], [u for _, _, u in pending])
        ok_items, ok_keys = [], []
        for (cid, platform, url), res in zip(pending, results):
            if clippo.eligible(res or {}):
                ok_items.append(clippo.submission_item(url, res))
                ok_keys.append((cid, platform))
            else:
                db.kv_set(f"clippo_check:{cid}:{platform}", {"at": time.time(), "reason": clippo.reason(res or {})})
        if ok_items:
            await clippo.submit(camp["ext_id"], ok_items)
            for cid, platform in ok_keys:
                c = db.one("SELECT submitted FROM clips WHERE id=?", (cid,))
                sub = db.jload(c["submitted"], {}) or {}
                sub[platform] = time.time()
                db.update("clips", cid, {"submitted": db.jdump(sub)})
                db.kv_set(f"clippo_check:{cid}:{platform}", {"at": time.time(), "reason": ""})
            sent += len(ok_items)
            db.event(f"Submitted {len(ok_items)} clip link(s) to Clippo: {camp['title'][:60]}", "campaign")
            await push.send(f"🎯 Submitted {len(ok_items)} clip(s) to Clippo", camp["title"][:90], url="/#campaigns",
                            tag=f"sub{camp['id']}")
    return sent


async def refresh(notify: bool = True) -> int:
    first_run = not db.one("SELECT 1 FROM campaigns LIMIT 1")
    new = upsert(await fetch_clippo(), "clippo")
    try:
        await sync_joined()
    except Exception as e:  # noqa: BLE001 - the public list must refresh even if the session expired
        log.warning("clippo joined sync: %s", e)
    db.kv_set("campaigns_refreshed", time.time())
    if notify and not first_run:
        for c in new[:5]:
            warn = " ⚠️ needs edits" if {"needs_audio", "custom_edit"} & set(c["flags"]) else ""
            await push.send(f"🎯 New Clippo campaign: Rp{c['rate']:,}/1k views", f"{c['title'][:80]}{warn}",
                            url="/#campaigns", tag=f"camp{c['ext_id']}")
    return len(new)


def youtube_footage(c: dict) -> list[str]:
    """Footage links Clip Studio can fetch by itself (YouTube videos); the rest the owner downloads + uploads."""
    return [f["url"] for f in c["footage"] if re.search(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)", f["url"])]


def hashtags_for_source(src: dict) -> list[str]:
    if not src.get("campaign_id"):
        return []
    r = db.one("SELECT hashtags FROM campaigns WHERE id=?", (src["campaign_id"],))
    return db.jload(r["hashtags"], []) if r else []
