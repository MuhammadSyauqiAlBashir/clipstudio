"""Downloading and recording with yt-dlp (YouTube needs deno for its JS challenge; installed system-wide).
Cookies from the throwaway account are used only after YouTube asks to sign in ("not a bot"), and only from a
copy, so the stored file is never rewritten. Kick: normal download only; a Cloudflare block fails with the reason."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from . import config

log = logging.getLogger("cs.fetch")

BOT_BLOCK = ("Sign in to confirm", "not a bot", "confirm you're not a bot", "HTTP Error 429")
FORMAT_SORT = "res:1080,vcodec:h264,acodec:m4a"


class FetchError(Exception):
    pass


def platform_of(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "twitch.tv" in u:
        return "twitch"
    if "kick.com" in u:
        return "kick"
    return "other"


def env() -> dict:
    e = dict(os.environ)
    cache = config.STATE_DIR / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    e.update({"XDG_CACHE_HOME": str(cache), "DENO_DIR": str(cache / "deno"), "HOME": str(config.STATE_DIR),
              "PATH": e.get("PATH", "/usr/local/bin:/usr/bin:/bin")})
    return e


def _cookie_copy(work: Path) -> Path | None:
    try:
        if config.COOKIES_FILE.exists():
            dst = work / "cookies.txt"
            shutil.copyfile(config.COOKIES_FILE, dst)
            os.chmod(dst, 0o600)
            return dst
    except OSError as e:
        log.warning("cookies unreadable: %s", e)
    return None


async def _run(args: list[str], work: Path, progress=None, timeout: float = 4 * 3600) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--newline", "--no-playlist", "--no-colors",
           "--socket-timeout", "30", "--retries", "5", "--fragment-retries", "10", *args]
    p = await asyncio.create_subprocess_exec(*cmd, cwd=work, env=env(), stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT, limit=32 * 1024 * 1024)
    tail: list[str] = []
    started = time.monotonic()
    assert p.stdout is not None
    while True:
        try:
            line = await asyncio.wait_for(p.stdout.readline(), timeout=600)
        except asyncio.TimeoutError:
            p.kill()
            return 1, "yt-dlp stalled for 10 minutes"
        if not line:
            break
        s = line.decode(errors="replace").rstrip()
        if s.startswith("[cs] "):
            if progress:
                try:
                    done, total = s[5:].split()
                    progress(float(done) / float(total))
                except (ValueError, ZeroDivisionError):
                    pass
            continue
        tail = (tail + [s])[-30:]
        if time.monotonic() - started > timeout:
            p.kill()
            return 1, "download took too long"
    return await p.wait(), "\n".join(tail)


async def ytdlp(args: list[str], work: Path, progress=None, timeout: float = 4 * 3600) -> str:
    """Run yt-dlp; on a bot block, retry once with the throwaway cookies (if present)."""
    code, out = await _run(args, work, progress, timeout)
    if code != 0 and any(b in out for b in BOT_BLOCK):
        cookies = _cookie_copy(work)
        if cookies:
            log.info("bot check hit, retrying with cookies")
            code, out = await _run(["--cookies", str(cookies), *args], work, progress, timeout)
            cookies.unlink(missing_ok=True)
        else:
            out += "\n(YouTube asked to sign in; no throwaway cookies file is installed)"
    if code != 0:
        lines = [x for x in out.splitlines() if "ERROR" in x] or out.splitlines()[-3:]
        raise FetchError("\n".join(lines)[-800:] or "yt-dlp failed")
    return out


async def info(url: str, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    out = await ytdlp(["-J", "--skip-download", url], work, timeout=300)
    start = out.find("{")
    try:
        data = json.loads(out[start:out.rfind("}") + 1])
    except ValueError as e:
        raise FetchError("could not read the video's details") from e
    return {
        "id": str(data.get("id") or ""),
        "title": data.get("title") or data.get("fulltitle") or "",
        "creator": data.get("uploader") or data.get("channel") or data.get("uploader_id") or "",
        "creator_url": data.get("channel_url") or data.get("uploader_url") or "",
        "channel_id": data.get("channel_id") or data.get("uploader_id") or "",
        "duration": float(data.get("duration") or 0),
        "live_status": data.get("live_status") or ("is_live" if data.get("is_live") else ""),
        "description": (data.get("description") or "")[:3000],
        "platform": platform_of(data.get("webpage_url") or url),
        "url": data.get("webpage_url") or url,
    }


async def download(url: str, work: Path, max_seconds: float, duration: float, progress=None) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("source.*"):
        if old.suffix in (".part", ".ytdl") or ".part" in old.name:
            old.unlink(missing_ok=True)
    args = ["-S", FORMAT_SORT, "--merge-output-format", "mkv", "-o", "source.%(ext)s",
            "--progress-template", "download:[cs] %(progress.downloaded_bytes)s %(progress.total_bytes_estimate)s",
            "--concurrent-fragments", "2"]
    if duration and duration > max_seconds:
        args += ["--download-sections", f"*0-{int(max_seconds)}"]
    await ytdlp([*args, url], work, progress)
    files = [f for f in work.glob("source.*") if not f.name.endswith((".part", ".ytdl", ".json"))]
    if not files:
        raise FetchError("download finished but no file was found")
    return max(files, key=lambda f: f.stat().st_size)


async def record(url: str, work: Path, max_seconds: float, platform: str, stop: asyncio.Event | None = None) -> Path:
    """Record a live stream (from its start where YouTube allows) until it ends or `max_seconds` pass.
    Stopped with SIGINT so yt-dlp finishes the file; MPEG-TS so a cut-off file is still playable."""
    work.mkdir(parents=True, exist_ok=True)
    args = ["-S", FORMAT_SORT, "--hls-use-mpegts", "-o", "live.%(ext)s", "--no-part"]
    if platform == "youtube":
        args.append("--live-from-start")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--newline", "--no-playlist", "--no-colors",
           "--socket-timeout", "30", "--retries", "10", "--fragment-retries", "20", *args, url]
    logf = (work / "record.log").open("wb")
    p = await asyncio.create_subprocess_exec(*cmd, cwd=work, env=env(), stdout=asyncio.subprocess.DEVNULL, stderr=logf)
    deadline = time.monotonic() + max_seconds
    try:
        while p.returncode is None:
            if time.monotonic() > deadline or (stop and stop.is_set()):
                p.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(p.wait(), timeout=120)
                except asyncio.TimeoutError:
                    p.kill()
                break
            try:
                await asyncio.wait_for(p.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        p.send_signal(signal.SIGINT)
        raise
    finally:
        logf.close()
    err = (work / "record.log").read_text(errors="replace")[-20000:]
    files = [f for f in work.glob("live.*") if f.stat().st_size > 1_000_000]
    if not files:
        msg = re.findall(r"ERROR.*", err)
        raise FetchError((msg[-1] if msg else "recording produced no file")[:500])
    return max(files, key=lambda f: f.stat().st_size)
