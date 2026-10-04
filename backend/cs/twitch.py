"""Twitch: EventSub webhooks (stream.online / stream.offline; cost 1 each, app token) + a Helix poll as a
safety net, new uploads via /videos, and the archived VOD as the replay of a stream we couldn't record."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import httpx

from . import config, db, yt

log = logging.getLogger("cs.twitch")

HELIX = "https://api.twitch.tv/helix"
_token: dict = {"value": "", "exp": 0.0}


class TwitchError(Exception):
    pass


def configured() -> bool:
    return bool(config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET)


def secret() -> str:
    return hmac.new(yt.secret().encode(), b"twitch-eventsub", hashlib.sha256).hexdigest()[:48]


def verify(headers: dict, body: bytes) -> bool:
    mid = headers.get("twitch-eventsub-message-id", "")
    ts = headers.get("twitch-eventsub-message-timestamp", "")
    sig = headers.get("twitch-eventsub-message-signature", "")
    if not (mid and ts and sig):
        return False
    good = "sha256=" + hmac.new(secret().encode(), mid.encode() + ts.encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


async def token(force: bool = False) -> str:
    if not configured():
        raise TwitchError("Twitch keys are not set.")
    if not force and _token["value"] and _token["exp"] > time.time() + 300:
        return _token["value"]
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": config.TWITCH_CLIENT_ID, "client_secret": config.TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"})
    if r.status_code != 200:
        raise TwitchError(f"Twitch token {r.status_code}: {r.text[:200]}")
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + float(d.get("expires_in", 3600)))
    return _token["value"]


async def helix(method: str, path: str, params=None, json=None) -> dict:
    for attempt in range(2):
        tok = await token(force=attempt > 0)
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.request(method, f"{HELIX}{path}", params=params, json=json,
                                   headers={"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {tok}"})
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code >= 400:
            raise TwitchError(f"Twitch {path} {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}
    raise TwitchError("Twitch auth failed")


async def resolve(text: str) -> dict:
    login = text.strip().rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()
    data = (await helix("GET", "/users", params={"login": login})).get("data") or []
    if not data:
        raise TwitchError("Twitch channel not found")
    u = data[0]
    return {"ext_id": u["id"], "title": u["display_name"], "handle": u["login"], "url": f"https://www.twitch.tv/{u['login']}"}


async def live_streams(user_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(user_ids), 100):
        params = [("user_id", u) for u in user_ids[i:i + 100]] + [("first", "100")]
        for s in (await helix("GET", "/streams", params=params)).get("data") or []:
            if s.get("type") == "live":
                out[s["user_id"]] = s
    return out


async def videos(user_id: str, kind: str = "upload", first: int = 5) -> list[dict]:
    return (await helix("GET", "/videos", params={"user_id": user_id, "type": kind, "first": first})).get("data") or []


def vod_seconds(d: str) -> float:
    """Twitch durations look like '3h8m33s'."""
    total, num = 0.0, ""
    for ch in d or "":
        if ch.isdigit():
            num += ch
        elif ch in "hms" and num:
            total += int(num) * {"h": 3600, "m": 60, "s": 1}[ch]
            num = ""
    return total


async def subscribe(user_id: str) -> list[str]:
    ids = []
    for kind in ("stream.online", "stream.offline"):
        try:
            d = await helix("POST", "/eventsub/subscriptions", json={
                "type": kind, "version": "1", "condition": {"broadcaster_user_id": user_id},
                "transport": {"method": "webhook", "callback": f"{config.PUBLIC_URL}/api/twitch/callback",
                              "secret": secret()}})
            ids += [s["id"] for s in d.get("data") or []]
        except TwitchError as e:
            if "409" not in str(e):  # 409 = already subscribed
                raise
    return ids


async def unsubscribe_all(user_id: str):
    d = await helix("GET", "/eventsub/subscriptions", params={"user_id": user_id})
    for s in d.get("data") or []:
        await helix("DELETE", "/eventsub/subscriptions", params={"id": s["id"]})
