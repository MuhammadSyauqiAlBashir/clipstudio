"""Kick (best effort): official public API with an app token; live status is polled (Kick's API has no VOD
listing, so Kick channels are watched for live streams only). Downloads use the normal tools: if Kick's
Cloudflare blocks them, the source fails with the reason (never bypassed)."""

from __future__ import annotations

import time

import httpx

from . import config

API = "https://api.kick.com/public/v1"
_token: dict = {"value": "", "exp": 0.0}


class KickError(Exception):
    pass


def configured() -> bool:
    return bool(config.KICK_CLIENT_ID and config.KICK_CLIENT_SECRET)


async def token(force: bool = False) -> str:
    if not configured():
        raise KickError("Kick keys are not set.")
    if not force and _token["value"] and _token["exp"] > time.time() + 300:
        return _token["value"]
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post("https://id.kick.com/oauth/token", data={
            "grant_type": "client_credentials", "client_id": config.KICK_CLIENT_ID,
            "client_secret": config.KICK_CLIENT_SECRET})
    if r.status_code != 200:
        raise KickError(f"Kick token {r.status_code}: {r.text[:200]}")
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + float(d.get("expires_in", 3600)))
    return _token["value"]


async def api(path: str, params=None) -> dict:
    for attempt in range(2):
        tok = await token(force=attempt > 0)
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {tok}",
                                                                       "Accept": "application/json"})
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code >= 400:
            raise KickError(f"Kick {path} {r.status_code}: {r.text[:200]}")
        return r.json()
    raise KickError("Kick auth failed")


async def channels(slugs: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(slugs), 50):
        params = [("slug", s) for s in slugs[i:i + 50]]
        out += (await api("/channels", params=params)).get("data") or []
    return out


async def resolve(text: str) -> dict:
    slug = text.strip().rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()
    data = await channels([slug])
    if not data:
        raise KickError("Kick channel not found")
    c = data[0]
    s = c.get("slug") or slug
    return {"ext_id": str(c.get("broadcaster_user_id") or ""), "title": s, "handle": s, "url": f"https://kick.com/{s}"}
