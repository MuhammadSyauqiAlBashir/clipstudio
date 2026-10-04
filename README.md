# Clip Studio

Private clipping tool: long videos (pasted links, uploaded campaign files, and automatically watched YouTube, Twitch
and Kick channels — new uploads and live streams) → best moments as 9:16 clips with karaoke captions → owner review
(540×960 previews) → full-quality 1080×1920 render on approval → manual posting (save/share + copy caption).
Live at https://clips.bashir.my.id (login: `bashirsyauqi`, `bells`).

**Status: Phases 1–3 built and deployed 2026-10-04.** Publishing APIs (Phase 4) not built: posting is manual.

| Doc | What |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Working context for Claude sessions: rules, decisions, server facts, open tasks, history |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Plan + interview answers: architecture, data, pipeline, watchers, budget, phases |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Findings with sources |
| [`docs/SPEC-original.md`](docs/SPEC-original.md) | The owner's original build spec |

Hard rules: permission-based sources only (default blocked), no movie/TV/studio/sports, music excluded, nothing
publishes without approval, credit creators.

## How it works

```
link / upload / watcher ─▶ gate ─▶ yt-dlp (≤1080p, ≤4 h) ─▶ Groq Whisper (words) ─▶ FFmpeg loudness
  ─▶ Gemini picks moments (word ranges, hook, caption) ─▶ snap to sentences ─▶ Gemini audio check (music → out,
     laughter → bonus) ─▶ top N ─▶ YuNet faces → layout + crop path ─▶ 540×960 preview + .ass captions ─▶ push
  ─▶ owner approves ─▶ 1080×1920 final ─▶ save/share on the phone + copy caption per platform
```

| Part | Where |
|---|---|
| Web/API | `backend/cs/main.py` — `clipstudio.service`, 127.0.0.1:8400, user `clipstudio` |
| Worker | `backend/cs/worker.py` — `clipstudio-worker.service` (1 core, ≤900 MB, nice 10, idle I/O): one processing job at a time + one live recording, watchers, hourly cleanup |
| Pipeline | `pipeline.py` (steps, resumable), `fetch.py` (yt-dlp), `groq.py`, `moments.py` (Gemini + scoring), `faces.py`, `captions.py`, `render.py` |
| Watchers | `watch.py` + `yt.py` (WebSub + RSS + `videos.list`), `twitch.py` (EventSub + Helix), `kick.py` (API poll, live only) |
| Data | Own SQLite `/var/lib/clipstudio/clipstudio.db` (WAL); work files `/var/lib/clipstudio/work/s<id>/`, finals `/var/lib/clipstudio/clips/` |
| Login | Shared PocketBase `users`, **read-only** (password check + refresh); only `CS_ALLOWED_USERS` |
| Front end | `web/` — static PWA (plain ES modules), served by Caddy from `/srv/clipstudio` |
| Secrets | `/etc/clipstudio/env` (`root:clipstudio 640`): `GEMINI_API_KEY`, `GROQ_API_KEY`, `YT_API_KEY`, optional `TWITCH_CLIENT_ID/SECRET`, `KICK_CLIENT_ID/SECRET`; cookies `/etc/clipstudio/youtube-cookies.txt` (optional) |

## Deploy

```bash
./deploy/deploy.sh      # safe to re-run: venv, code, web, units, Caddy block (once, with backup), restarts
```

One-time server tools (already installed 2026-10-04): `ffmpeg` (apt) and `deno` in `/usr/local/bin` (GitHub
release, checksum verified). yt-dlp updates itself daily (`clipstudio-ytdlp-update.timer`).

Add a key later (hidden input, never printed), then restart:
```bash
k=TWITCH_CLIENT_ID; read -rsp "Paste $k: " V; echo; sudo sed -i "/^$k=/d" /etc/clipstudio/env; printf '%s=%s\n' "$k" "$V" | sudo tee -a /etc/clipstudio/env >/dev/null; unset V
sudo systemctl restart clipstudio clipstudio-worker
```

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt pytest
.venv/bin/pytest -q tests
```
`~/work/clipstudio-dev/dev.sh script.py` runs code with the real keys against a scratch state dir, capped like the worker.

## Logs and checks

```bash
journalctl -u clipstudio -u clipstudio-worker -f
systemctl is-active clipstudio clipstudio-worker
```
