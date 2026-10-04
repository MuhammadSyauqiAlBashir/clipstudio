"""Clip Studio's own SQLite database (WAL), shared by the web and worker processes.
It lives in the app's state dir, so the shared PocketBase (finance, shop, games, lyrsync) is never touched."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,              -- youtube / twitch / kick
  ext_id TEXT NOT NULL DEFAULT '',     -- YouTube UC…, Twitch user id, Kick broadcaster id
  handle TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  permission TEXT NOT NULL DEFAULT 'blocked',  -- explicit / platform-default / campaign / implied / blocked
  proof TEXT NOT NULL DEFAULT '',
  campaign TEXT NOT NULL DEFAULT '{}', -- {"name","rate","url"}
  watch_uploads INTEGER NOT NULL DEFAULT 1,
  watch_live INTEGER NOT NULL DEFAULT 1,
  min_minutes REAL NOT NULL DEFAULT 5,
  enabled INTEGER NOT NULL DEFAULT 1,
  sub TEXT NOT NULL DEFAULT '{}',      -- push subscription state per platform
  live_now INTEGER NOT NULL DEFAULT 0,
  last_checked REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(platform, ext_id)
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER,
  platform TEXT NOT NULL DEFAULT '',   -- youtube / twitch / kick / upload / other
  url TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'manual', -- manual / upload / vod / live / replay
  title TEXT NOT NULL DEFAULT '',
  creator TEXT NOT NULL DEFAULT '',
  creator_url TEXT NOT NULL DEFAULT '',
  duration REAL NOT NULL DEFAULT 0,
  permission TEXT NOT NULL DEFAULT 'blocked',
  proof TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  step TEXT NOT NULL DEFAULT '',
  progress REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  file TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  files_deleted INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  added_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sources_status ON sources(status);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                  -- process / final / record
  source_id INTEGER,
  clip_id INTEGER,
  priority INTEGER NOT NULL DEFAULT 0, -- higher first: final 30, manual 20, auto 10
  status TEXT NOT NULL DEFAULT 'queued', -- queued / running / done / failed / cancelled
  not_before REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  wait_reason TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  started_at REAL NOT NULL DEFAULT 0,
  finished_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, kind);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  word_from INTEGER NOT NULL DEFAULT 0,
  word_to INTEGER NOT NULL DEFAULT 0,
  llm_score REAL NOT NULL DEFAULT 0,
  loud REAL NOT NULL DEFAULT 0,
  laughter INTEGER NOT NULL DEFAULT 0,
  reaction INTEGER NOT NULL DEFAULT 0,
  music TEXT NOT NULL DEFAULT '',      -- none / faint / clear / song ('' = not checked)
  score REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  hook TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL DEFAULT '',
  hashtags TEXT NOT NULL DEFAULT '',
  owner_text TEXT NOT NULL DEFAULT '',
  layout TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate', -- candidate/excluded/review/approved/rendering/ready/rejected/expired/posted
  note TEXT NOT NULL DEFAULT '',       -- why excluded / render error
  reject_reason TEXT NOT NULL DEFAULT '',
  preview TEXT NOT NULL DEFAULT '',
  thumb TEXT NOT NULL DEFAULT '',
  final TEXT NOT NULL DEFAULT '',
  final_size INTEGER NOT NULL DEFAULT 0,
  posted TEXT NOT NULL DEFAULT '{}',   -- {"tiktok": url, "youtube": url, "instagram": url}
  views TEXT NOT NULL DEFAULT '{}',
  decided_by TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  decided_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS clips_source ON clips(source_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  kind TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  source_id INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL,
  key TEXT NOT NULL,
  value REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(day, key)
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subs (
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  ua TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (     -- webhook deliveries handed from the web process to the worker
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (     -- one row per clip and platform: a retry never posts twice
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL,
  platform TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', -- queued / uploading / processing / done / failed
  remote_id TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before REAL NOT NULL DEFAULT 0,
  posted_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(clip_id, platform)
);
CREATE TABLE IF NOT EXISTS seen (
  platform TEXT NOT NULL,
  video_id TEXT NOT NULL,
  at REAL NOT NULL,
  PRIMARY KEY(platform, video_id)
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "clips_per_hour": 8,
    "min_clip_seconds": 15,
    "max_clip_seconds": 60,
    "score_threshold": 55,
    "music_allowed": "none",          # strictest music level still allowed: none / faint
    "hashtags": "#clips #fyp",
    "caption_template": "{hook}\n\n{caption}\n\n🎥 {creator} — {source_url}\n{hashtags}",
    "keyword_blocklist": "full movie, full episode, movie clip, trailer, official music video, lyrics, karaoke, "
                         "premier league, la liga, nba, nfl, uefa, fifa, liga 1, highlights pertandingan, "
                         "sinetron, ftv",
    "max_source_hours": config.MAX_SOURCE_HOURS,
    "auto_max_age_hours": 48,
    "autopost": {"instagram": True, "tiktok": True, "youtube": False},
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _conn = c
    return _conn


def reset_for_tests():
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def execute(sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
    with _lock:
        return conn().execute(sql, args)


def all(sql: str, args: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple | list = ()) -> dict | None:
    with _lock:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def insert(table: str, data: dict) -> int:
    cols = ",".join(data)
    marks = ",".join("?" for _ in data)
    with _lock:
        return conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values())).lastrowid


def update(table: str, row_id: int, data: dict):
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    with _lock:
        conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), row_id])


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def jload(s: str | None, default: Any = None) -> Any:
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


# ---- settings / key-value ---------------------------------------------------------------------------
def kv_get(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM kv WHERE key=?", (key,))
    return jload(r["value"], default) if r else default


def kv_set(key: str, value: Any):
    execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, jdump(value)))


def settings() -> dict:
    return {**DEFAULT_SETTINGS, **(kv_get("settings", {}) or {})}


# ---- usage counters (quota guards) -----------------------------------------------------------------
def usage_add(key: str, value: float, day: str | None = None):
    execute("INSERT INTO usage(day,key,value) VALUES(?,?,?) ON CONFLICT(day,key) DO UPDATE SET value=value+excluded.value",
            (day or today(), key, value))


def usage_get(key: str, day: str | None = None) -> float:
    r = one("SELECT value FROM usage WHERE day=? AND key=?", (day or today(), key))
    return r["value"] if r else 0.0


def event(message: str, kind: str = "", level: str = "info", source_id: int | None = None):
    execute("INSERT INTO events(at,level,kind,message,source_id) VALUES(?,?,?,?,?)",
            (now(), level, kind, message[:2000], source_id))
    execute("DELETE FROM events WHERE at < ?", (now() - 30 * 86400,))


# ---- seen video ids (watcher dedupe) ---------------------------------------------------------------
def mark_seen(platform: str, video_id: str) -> bool:
    """True if this id is new (and marks it seen)."""
    with _lock:
        cur = conn().execute("INSERT OR IGNORE INTO seen(platform,video_id,at) VALUES(?,?,?)", (platform, video_id, now()))
        return cur.rowcount == 1


# ---- jobs ----------------------------------------------------------------------------------------------
def enqueue(kind: str, *, source_id: int | None = None, clip_id: int | None = None, priority: int = 10,
            not_before: float = 0) -> int:
    return insert("jobs", {"kind": kind, "source_id": source_id, "clip_id": clip_id, "priority": priority,
                           "not_before": not_before, "created_at": now()})
