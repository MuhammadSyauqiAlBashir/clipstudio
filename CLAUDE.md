# Clip Studio — Claude context

A private tool for Bashir (owner, app username `bashirsyauqi`) to try a **clipping side hustle**: long videos (pasted
links, uploaded campaign files + automatically watched **YouTube, Twitch and Kick** channels, both **new uploads/VODs
and live streams**) → the best moments as 9:16 clips with captions → the owner reviews and approves → posts (manually
at first). Planned at `https://clips.bashir.my.id`.

**Status (2026-10-04): Phases 1–3 built and LIVE at https://clips.bashir.my.id** (paste link / upload → clips →
review → 1080×1920 final → manual posting; YouTube/Twitch/Kick watchers; music check). Phase 4 (publishing APIs) not
built. As-built differences from the plan: `docs/DESIGN.md` §13. Code map + deploy: `README.md`.

Read first:
- `docs/DESIGN.md`: the adapted plan (architecture, data model, pipeline, watchers, server budget, free services, phases, open questions).
- `docs/RESEARCH.md`: every finding with sources (open-source clippers, Groq/Gemini limits, WebSub/RSS, yt-dlp/ytarchive, music/laughter detection, platform APIs, money).
- `docs/SPEC-original.md`: the owner's original spec, word for word.

Server-wide facts are in `~/.claude/CLAUDE.md`. The original conversation that led here (2026-10-03/04) is in the
general session transcript `~/.claude/projects/-home-bashir/b434ae8c-ff15-4ca3-aa6d-842e57f5a2aa.jsonl`; owner
messages are extracted in `~/work/tx_user.txt` (it only covers up to 2026-10-03; the clip discussion is at the end of
the .jsonl).

## Rules for working on this app

- **Hard rules (owner's spec):** permission-based sources only (per-channel `clip_permission`, default **blocked**);
  no movie/TV/studio/sports footage; music segments excluded; **nothing publishes without the owner's approval**;
  credit the original creator.
- **Never hurt the live apps** on this server (finance has real money data): the worker runs with `Nice=10`,
  `CPUQuota=100%` (1 of 2 cores), `MemoryMax≈900M`, idle I/O; refuse downloads when free disk < 10 GB.
- **One processing job at a time** (+ at most one live recording beside it); final renders of approved clips go before
  new sources. The source video is kept until all its clips are decided or **7 days**, then deleted.
- **Kick:** official API for detection, normal yt-dlp/streamlink for downloads. **Never bypass Cloudflare** (no
  cloudscraper); a blocked Kick source fails with the reason.
- **YouTube cookies:** only from **one throwaway account**, only when downloads are blocked, never mixed with the
  owner's real accounts. If it gets banned, stop and ask the owner; never create accounts automatically.
- Free services only (owner is budget-conscious). Anything billable (paid APIs, server upgrade) needs the owner's OK.
- Keep Clip Studio **off** Cloudflare Workers AI (the shop's FLUX budget) and Shazam (lyrsync's budget).
- Gemini: use **only Clip Studio's own project key** (`clipstudio-ai`). One project per app is the owner's rule;
  never create extra projects per feature to multiply quota (Google's terms).
- Never print secrets. Owner prefers step-by-step, click-by-click guidance.
- Git: repo `MuhammadSyauqiAlBashir/clipstudio` (private), work on `develop`, commit as
  `-c user.name="Bashir" -c user.email="bashirsyauqi@gmail.com"`. **BashGames flow** (owner, 2026-10-04): push
  `develop` → PR to `main` → merge, via the GitHub REST API (token in `~/.git-credentials`; `gh` isn't installed).
- Follow the new-app checklist in `~/.claude/CLAUDE.md` §8 (own user, sandboxed unit, `/etc/clipstudio/env`,
  PocketBase prefix `clip_` + machine login with its own role, Caddy block in `deploy/`, deploy script, tests, README).
- Keep this file updated (status, decisions, history, open tasks).

## Decisions so far (from the general session, 2026-10-03/04)

- The owner asked whether the spec is too heavy for the server and whether it's worth it as a side hustle. Answer
  given and accepted:
  - **The full spec as written is too heavy, but a lean version fits for Rp0 extra.**
  - **Worth trying as a cheap experiment.** Realistic income comes from clipping campaigns (paid per view;
    averages are low). Native monetization is far off.
- Lean v1 = paste link (or upload a campaign file) → transcribe with **Groq** (free) → **Gemini** picks moments →
  FFmpeg + MediaPipe render 9:16 with captions → review site → **manual posting** (download + copy caption).
- The owner then asked to also **automatically watch specific YouTube channels: live streams and new uploads**. The
  plan uses WebSub push + RSS polling + `videos.list` (1 quota unit); recording uses yt-dlp `--live-from-start` /
  ytarchive-style. See DESIGN §5.
- Separate Gemini project created by the owner: **`clipstudio-ai`**. The key is stored in `/etc/clipstudio/env`
  (currently `root:root 600`; when the app user exists: `chown root:clipstudio` + `chmod 640`). Tested OK 2026-10-04
  (`gemini-3.1-flash-lite` answered).
- Don't install OpenShorts/SupoClip (8 GB+ RAM / Docker / Postgres+Redis / AGPL). Borrow ideas: word-index moments
  (AutoClip), layout modes (OpenShorts), `.ass` karaoke captions (ffmpeg-caption-burn-ass).
- YouTube cut reach for unoriginal reposted Shorts on **2026-10-01** → each clip needs added value (hook title,
  branding, later owner commentary); campaigns with authorised footage preferred.
- **Interview 2026-10-04 (full table in DESIGN §11):** sources = YouTube channels + clipping campaigns + Twitch +
  Kick, all three platforms equally (Kick best effort); post on TikTok, YouTube Shorts, Instagram Reels from **new**
  accounts; language auto (ID/EN); karaoke captions; **540×960 preview for review, final 1080×1920 rendered only after
  approval** (owner's idea); up to 8 clips per hour, 15–60 s; live max 4 h, 1 recording at a time, a second live →
  download its replay; throwaway YouTube cookies OK (rule above); auto hook + credit + optional owner text; Web Push +
  review on the phone; login `bashirsyauqi` and `bells`; BashGames git flow.
- From the handoff review: the Gemini **music check is in Phase 1** (hard rule); phase order = watchers 2nd, scoring
  3rd, publishing 4th (publishing is manual until the platform audits pass anyway).
- The Google Cloud project `clipstudio-ai` (Gemini + YouTube Data API keys) is on the owner's **finance Google
  account**, not bashirsyauqi@gmail.com.

## Server facts (built 2026-10-04)

| Item | Value |
|---|---|
| Web/API | `clipstudio.service`, user `clipstudio`, 127.0.0.1:**8400**, code `/opt/clipstudio/cs`, venv `/opt/clipstudio/venv` |
| Worker | `clipstudio-worker.service` (same user): `Nice=10`, `CPUQuota=100%`, `MemoryMax=900M`, idle I/O; no `MemoryDenyWriteExecute` (deno JIT) |
| yt-dlp | updated daily by `clipstudio-ytdlp-update.timer` (05:20); deno 2.9.7 in `/usr/local/bin` (checksum verified); ffmpeg 6.1 (apt) |
| Data | **Own SQLite** `/var/lib/clipstudio/clipstudio.db` (tables channels, sources, jobs, clips, events, usage, kv, push_subs, seen, inbox); work `/var/lib/clipstudio/work/s<id>/`, finals `/var/lib/clipstudio/clips/`, VAPID key `/var/lib/clipstudio/vapid_private.pem`. **No PocketBase collections**; login reads the shared `users` only |
| Secrets | `/etc/clipstudio/env` (`root:clipstudio 640`): `GEMINI_API_KEY`, `GROQ_API_KEY`, `YT_API_KEY` (restricted to YouTube Data API v3 + IP 103.103.21.9); later `TWITCH_CLIENT_ID/SECRET`, `KICK_CLIENT_ID/SECRET`. Optional cookies `/etc/clipstudio/youtube-cookies.txt` (deploy sets `root:clipstudio 640`). `CS_WEBSUB_SECRET` unset → random one in the DB `kv` |
| Site | `clips.bashir.my.id` block appended to `/etc/caddy/Caddyfile` (backup `~/work/Caddyfile.bak-20261004-141255`); source `deploy/Caddyfile.clipstudio`; uploads up to 8 GB; public signed callbacks `/api/websub/callback`, `/api/twitch/callback` |
| Static | `/srv/clipstudio` |
| Dev | `.venv` in the repo; `~/work/clipstudio-dev/dev.sh <script.py>` runs with the real keys against `~/work/clipstudio-dev/state`, capped like the worker. (Don't use `~/work/cs-dev`: it belongs to another project.) |
| Checks | `systemctl is-active clipstudio clipstudio-worker`; `journalctl -u clipstudio-worker -f`; More tab in the app shows worker, queue, quotas, disk, keys, activity |

## Open owner tasks (remind at session start)

Full click-by-click guide: `~/work/clipstudio-owner-setup.md`.
- [ ] iPhone: open https://clips.bashir.my.id in Safari → Share → Add to Home Screen → open it → More → Turn on
      notifications. Try the review flow on the test clips (source added by Claude: a CC-BY interview).
- [ ] Clip brand name + new Google account (clips) + YouTube channel, TikTok, Instagram (Creator) — before posting.
- [ ] Twitch account + 2FA + developer app → `TWITCH_CLIENT_ID/SECRET` (hidden-input command), then
      `sudo systemctl restart clipstudio clipstudio-worker`.
- [ ] Kick account + 2FA + developer app → `KICK_CLIENT_ID/SECRET` (same). Kick webhooks stay OFF (the app polls).
- [ ] Throwaway YouTube account → cookies file → `/etc/clipstudio/youtube-cookies.txt` (only used when YouTube blocks).
- [ ] Channel/campaign list (with proof of permission) → add in the Channels tab.
- Done 2026-10-04: interview; Groq + YouTube keys (tested); Biznet traffic is unlimited.

## History

- 2026-10-03 — Owner shared the build spec; feasibility + side-hustle verdict (lean version, Rp0, experiment);
  server/free-service impact analysed.
- 2026-10-04 — Separate Gemini projects per app (shop/games/clipstudio keys installed and tested). Repo cloned to
  `~/clipstudio`; research done (open-source clippers, watchers, recorders, detection models, platform rules, money);
  docs written (`CLAUDE.md`, `README.md`, `docs/DESIGN.md`, `docs/RESEARCH.md`, `docs/SPEC-original.md`) and pushed to `develop`.
- 2026-10-04 — First session in `~/clipstudio`: handoff checked (feedback `~/work/handoff-feedback-clipstudio-2026-10-04.md`,
  reply `~/work/handoff-reply-clipstudio-2026-10-04.md`); Groq + YouTube keys installed and tested; interview done;
  DESIGN updated (Twitch/Kick, preview-then-final render, music check in Phase 1, live concurrency, cookies rule).
- 2026-10-04 — Built and deployed Phases 1–3 (own SQLite, YuNet faces, yt-dlp only, Kick poll). Installed ffmpeg +
  deno. End-to-end tested on a CC-BY 30-min interview (dev and production sandbox): ~6 min per 30 min of video, final
  37 s clip ~70 s, worker peak ~690 MB; other apps unaffected. 20 tests pass. Owner's preview-then-final idea built.
