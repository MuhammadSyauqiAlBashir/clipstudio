"""Facebook Reels on the owner's Page (Graph API video_reels: start → upload the file → finish).
The Page token comes from the owner's long-lived user token and never expires (Meta app in dev mode, the owner is
the app admin and Page admin, so no app review is needed)."""

from __future__ import annotations

from pathlib import Path

import httpx

from . import config

API = "https://graph.facebook.com/v24.0"
UPLOAD = "https://rupload.facebook.com/video-upload/v24.0"


class FacebookError(Exception):
    pass


def connected() -> bool:
    return bool(config.FB_PAGE_ID and config.FB_PAGE_TOKEN)


def _err(r: httpx.Response) -> str:
    try:
        e = r.json().get("error", {})
        return f"Facebook {r.status_code}: {e.get('error_user_msg') or e.get('message') or r.text[:200]}"
    except ValueError:
        return f"Facebook {r.status_code}: {r.text[:200]}"


async def page_info() -> dict:
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/{config.FB_PAGE_ID}", params={"fields": "name,link,followers_count",
                                                                  "access_token": config.FB_PAGE_TOKEN})
    if r.status_code != 200:
        raise FacebookError(_err(r))
    return r.json()


async def start_and_upload(path: Path) -> str:
    """Create the reel and upload the file. Returns the video id (not published yet)."""
    data = path.read_bytes()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20)) as http:
        r = await http.post(f"{API}/{config.FB_PAGE_ID}/video_reels",
                            data={"upload_phase": "start", "access_token": config.FB_PAGE_TOKEN})
        if r.status_code != 200 or "video_id" not in r.json():
            raise FacebookError(_err(r))
        vid = r.json()["video_id"]
        r = await http.post(f"{UPLOAD}/{vid}", content=data, headers={
            "Authorization": f"OAuth {config.FB_PAGE_TOKEN}", "offset": "0", "file_size": str(len(data))})
        if r.status_code != 200 or not r.json().get("success"):
            raise FacebookError(_err(r))
    return vid


async def finish(video_id: str, description: str, state: str = "PUBLISHED"):
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(f"{API}/{config.FB_PAGE_ID}/video_reels", data={
            "upload_phase": "finish", "video_id": video_id, "video_state": state, "description": description[:2200],
            "access_token": config.FB_PAGE_TOKEN})
    if r.status_code != 200 or not r.json().get("success"):
        raise FacebookError(_err(r))


async def status(video_id: str) -> tuple[str, str, str]:
    """(video_status, publishing status, permalink)."""
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/{video_id}", params={"fields": "status,permalink_url",
                                                         "access_token": config.FB_PAGE_TOKEN})
    if r.status_code != 200:
        raise FacebookError(_err(r))
    d = r.json()
    st = d.get("status") or {}
    pub = (st.get("publishing_phase") or {}).get("status", "")
    link = d.get("permalink_url") or ""
    if link.startswith("/"):
        link = "https://www.facebook.com" + link
    return st.get("video_status", ""), pub, link


async def delete(video_id: str):
    async with httpx.AsyncClient(timeout=20) as http:
        await http.delete(f"{API}/{video_id}", params={"access_token": config.FB_PAGE_TOKEN})
