"""Web Push (same approach as finance/shop/games): VAPID key in the private state dir, Urgency high so iOS delivers
right away. Subscriptions live in Clip Studio's own database."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import config, db

log = logging.getLogger("cs.push")
_vapid: Vapid | None = None


def vapid() -> Vapid:
    global _vapid
    if _vapid is None:
        path = config.STATE_DIR / "vapid_private.pem"
        if path.exists():
            _vapid = Vapid.from_file(str(path))
        else:
            v = Vapid()
            v.generate_keys()
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            v.save_key(str(path))
            path.chmod(0o600)
            _vapid = v
    return _vapid


def public_key() -> str:
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _send_one(sub: dict, payload: str) -> int:
    try:
        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=payload, vapid_private_key=vapid(), vapid_claims={"sub": config.VAPID_SUBJECT}, ttl=24 * 3600,
                timeout=15, headers={"Urgency": "high"})
        return 201
    except WebPushException as e:
        return e.response.status_code if e.response is not None else 0
    except Exception as e:  # noqa: BLE001 - a push must never break the pipeline
        log.warning("push failed: %s", e)
        return 0


async def send(title: str, body: str, url: str = "/", tag: str = "", user: str | None = None) -> int:
    subs = db.all("SELECT * FROM push_subs" + (" WHERE user=?" if user else ""), (user,) if user else ())
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url})
    sent = 0
    for sub in subs:
        status = await asyncio.to_thread(_send_one, sub, payload)
        log.info("push %r -> %s: %s", title[:40], sub["endpoint"][8:30], status)
        if status in (404, 410):
            db.execute("DELETE FROM push_subs WHERE id=?", (sub["id"],))
        elif status in (200, 201):
            sent += 1
    return sent
