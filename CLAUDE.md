# Clip Studio — Claude context

A private tool for Bashir (owner, app username `bashirsyauqi`) to try a **clipping side hustle**: long videos (pasted
links, uploaded campaign files + automatically watched **YouTube, Twitch and Kick** channels, both **new uploads/VODs
and live streams**) → the best moments as 9:16 clips with captions → the owner reviews and approves → posts (manually
at first). Planned at `https://clips.bashir.my.id`.

**Status (2026-10-04): docs only, nothing built. Interview done (answers in `docs/DESIGN.md` §11).** Next: Phase 1
(DESIGN §10).

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

## Planned server facts (not created yet)

| Item | Plan |
|---|---|
| Web/API | `clipstudio.service`, user `clipstudio`, 127.0.0.1:**8400** (free port; others use 8000/8090/8100/8200/8210/8300) |
| Worker | `clipstudio-worker.service` (same user; limits above) |
| Data | PocketBase `clip_*`, machine login `svc_clipstudio`, role `clips`; add `clip_*` to the `pb.bashir.my.id` 404 list |
| Files | `/var/lib/clipstudio/{work,clips,models}` (StateDirectory) |
| Secrets | `/etc/clipstudio/env` (`root:root 600` until the app user exists): `GEMINI_API_KEY`, `GROQ_API_KEY`, `YT_API_KEY` (all present and tested 2026-10-04; YouTube key restricted to YouTube Data API v3 + IP 103.103.21.9). Later: PB login, `CS_WEBSUB_SECRET`, `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`, `KICK_CLIENT_ID`/`KICK_CLIENT_SECRET`, VAPID keys |
| Site | `clips.bashir.my.id` (DNS wildcard already points here); Caddy block `deploy/Caddyfile.clipstudio`; public (no login) callbacks: `/api/websub/callback`, `/api/twitch/callback`, `/api/kick/callback` (each verifies its signature) |
| Tools to install | ffmpeg (apt), yt-dlp (pip, keep it updated), streamlink, deno (for YouTube nsig), mediapipe (Python ≤ 3.12 wheels; the server's Python is 3.12.3). None installed yet |

## Open owner tasks (remind at session start)

- [ ] Check whether the Biznet plan has a monthly data-transfer limit (each hour of 1080p source downloads ~1.5–3 GB).
- [ ] List the first sources: YouTube/Twitch/Kick channels that allow clipping, and/or clipping campaigns (Whop
      Content Rewards, etc.).
- [ ] Before posting: create new TikTok, YouTube and Instagram accounts just for clips.
- [ ] Before Phase 2: free Twitch developer app (dev.twitch.tv, Twitch account with 2FA) and Kick developer app;
      Claude guides click by click and gives hidden-input commands for the keys.
- [ ] Only if YouTube blocks downloads: one throwaway YouTube account for cookies.
- Done 2026-10-04: interview; Groq account + key; YouTube Data API v3 enabled + restricted key (both tested).

## History

- 2026-10-03 — Owner shared the build spec; feasibility + side-hustle verdict (lean version, Rp0, experiment);
  server/free-service impact analysed.
- 2026-10-04 — Separate Gemini projects per app (shop/games/clipstudio keys installed and tested). Repo cloned to
  `~/clipstudio`; research done (open-source clippers, watchers, recorders, detection models, platform rules, money);
  docs written (`CLAUDE.md`, `README.md`, `docs/DESIGN.md`, `docs/RESEARCH.md`, `docs/SPEC-original.md`) and pushed to `develop`.
- 2026-10-04 — First session in `~/clipstudio`: handoff checked (feedback `~/work/handoff-feedback-clipstudio-2026-10-04.md`,
  reply `~/work/handoff-reply-clipstudio-2026-10-04.md`); Groq + YouTube keys installed and tested; interview done;
  DESIGN updated (Twitch/Kick, preview-then-final render, music check in Phase 1, live concurrency, cookies rule).
