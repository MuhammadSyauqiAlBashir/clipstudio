"""Gemini API client (REST, free tier, Clip Studio's own `clipstudio-ai` key only).
On a rate limit or overload it falls back to the next model. Same lessons as the other apps: no temperature on
Gemini 3, JSON schemas with every field required + propertyOrdering."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from . import config, db

log = logging.getLogger("cs.ai")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FAST = [config.GEMINI_FAST_MODEL, "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash"]
SMART = [config.GEMINI_SMART_MODEL, "gemini-3.6-flash", "gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash",
         "gemini-3.1-flash-lite"]

_http: httpx.AsyncClient | None = None


class AIUnavailable(Exception):
    """Every model refused (quota used up / overloaded / not configured). Retry later."""


def client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
    return _http


def audio_part(data: bytes, mime: str = "audio/mp3") -> dict:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}


def obj(props: dict[str, dict]) -> dict:
    """JSON schema object with every property required, in order."""
    return {"type": "OBJECT", "properties": props, "required": list(props), "propertyOrdering": list(props)}


def _dedupe(models: list[str]) -> list[str]:
    seen, out = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


async def generate(parts: list[Any], *, system: str = "", schema: dict | None = None, smart: bool = False,
                   max_tokens: int = 0) -> Any:
    """Call Gemini. With `schema`, returns parsed JSON; otherwise text."""
    if not config.GEMINI_API_KEY:
        raise AIUnavailable("Gemini is not configured.")
    contents = [{"role": "user", "parts": [p if isinstance(p, dict) else {"text": str(p)} for p in parts]}]
    gen: dict[str, Any] = {"maxOutputTokens": max_tokens or 8192}
    body: dict[str, Any] = {"contents": contents, "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = schema

    last_error = "no model answered"
    for model in _dedupe([*(SMART if smart else FAST)]):
        for attempt in range(2):
            try:
                r = await client().post(URL.format(model=model), json=body,
                                        headers={"x-goog-api-key": config.GEMINI_API_KEY})
            except httpx.HTTPError as e:
                last_error = f"{model}: {e}"
                await asyncio.sleep(2)
                continue
            db.usage_add("gemini_calls", 1)
            if r.status_code == 200:
                data = r.json()
                try:
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                except (KeyError, IndexError):
                    last_error = f"{model}: empty answer ({data.get('promptFeedback', {})})"
                    break
                if schema:
                    try:
                        return json.loads(text)
                    except ValueError:
                        last_error = f"{model}: invalid JSON"
                        break
                return text
            last_error = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            if r.status_code == 500 and attempt == 0:
                await asyncio.sleep(1)
                continue
            break  # 429 quota, 503 overloaded, 404 unknown model: next model
        log.warning("gemini fallback: %s", last_error)
    db.usage_add("gemini_failures", 1)
    raise AIUnavailable(last_error)
