"""Settings from the environment (systemd EnvironmentFile /etc/clipstudio/env + Environment= lines)."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jakarta")
DEV = os.environ.get("CS_DEV", "") == "1"

PB_URL = os.environ.get("CS_PB_URL", "http://127.0.0.1:8090")
PUBLIC_URL = os.environ.get("CS_PUBLIC_URL", "https://clips.bashir.my.id")
# People who may log in (shared PocketBase `users`, approved, role ''/admin). Nobody else, even if approved.
ALLOWED_USERS = {u.strip().lower() for u in os.environ.get("CS_ALLOWED_USERS", "bashirsyauqi,bells").split(",") if u.strip()}

STATE_DIR = Path(os.environ.get("CS_STATE_DIR", "/var/lib/clipstudio"))
DB_PATH = STATE_DIR / "clipstudio.db"
WORK_DIR = STATE_DIR / "work"      # sources, audio, transcripts, previews (per source)
CLIPS_DIR = STATE_DIR / "clips"    # final renders
ASSETS = Path(__file__).parent / "assets"
FONT_NAME = "Anton"

COOKIES_FILE = Path(os.environ.get("CS_COOKIES_FILE", "/etc/clipstudio/youtube-cookies.txt"))
SESSIONS_DIR = os.environ.get("CS_SESSIONS_DIR", "/etc/clipstudio/sessions")  # campaign-site sessions (owner's)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_FAST_MODEL = os.environ.get("CS_GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
GEMINI_SMART_MODEL = os.environ.get("CS_GEMINI_SMART_MODEL", "gemini-3.5-flash")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("CS_GROQ_MODEL", "whisper-large-v3-turbo")
YT_API_KEY = os.environ.get("YT_API_KEY", "")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
KICK_CLIENT_ID = os.environ.get("KICK_CLIENT_ID", "")
KICK_CLIENT_SECRET = os.environ.get("KICK_CLIENT_SECRET", "")
# Auto-posting (owner's own accounts only)
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")  # seed; refreshed copies live in the DB
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "")
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")  # sandbox keys until TikTok approves the app
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
YT_OAUTH_CLIENT_ID = os.environ.get("YT_OAUTH_CLIENT_ID", "")   # Google OAuth web client (project clipstudio-ai)
YT_OAUTH_CLIENT_SECRET = os.environ.get("YT_OAUTH_CLIENT_SECRET", "")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")        # Facebook Page bashclipeveryday
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")  # Page token (never expires), made from FB_USER_TOKEN
# Signs WebSub / EventSub deliveries. If unset, a random one is created in the state dir.
WEBHOOK_SECRET = os.environ.get("CS_WEBSUB_SECRET", "")
VAPID_SUBJECT = os.environ.get("CS_VAPID_SUBJECT", "mailto:admin@bashir.my.id")

# Guards (owner's rules: never hurt the live apps, free tiers only)
MIN_FREE_DISK_GB = float(os.environ.get("CS_MIN_FREE_DISK_GB", "10"))
GROQ_DAILY_SECONDS = int(os.environ.get("CS_GROQ_DAILY_SECONDS", "27000"))  # free tier: 28,800 s/day
MAX_JOB_ATTEMPTS = 3  # a failing job is tried this many times (10, 20 min apart) before it fails
YT_DAILY_UNITS = int(os.environ.get("CS_YT_DAILY_UNITS", "9800"))          # free: 10,000/day (one shared budget)
MAX_SOURCE_HOURS = float(os.environ.get("CS_MAX_SOURCE_HOURS", "4"))       # VODs and live recordings
KEEP_SOURCE_DAYS = 7
KEEP_CLIP_DAYS = 30
FFMPEG_THREADS = os.environ.get("CS_FFMPEG_THREADS", "2")
