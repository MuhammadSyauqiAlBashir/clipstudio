# Clip Studio — Claude context

A private tool for Bashir (owner, app username `bashirsyauqi`) to try a **clipping side hustle**: long videos (pasted
links + automatically watched YouTube channels, both **new uploads and live streams**) → the best moments as 9:16
clips with captions → the owner reviews and approves → posts (manually at first). Planned at
`https://clips.bashir.my.id`.

**Status (2026-10-04): docs only, nothing built.** Next: a short interview with the owner (questions in
`docs/DESIGN.md` §11), then Phase 1.

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
  `CPUQuota=100%` (1 of 2 cores), `MemoryMax≈900M`, idle I/O, **one job at a time**; refuse downloads when free disk
  < 10 GB; delete source videos after rendering.
- Free services only (owner is budget-conscious). Anything billable (paid APIs, server upgrade) needs the owner's OK.
- Keep Clip Studio **off** Cloudflare Workers AI (the shop's FLUX budget) and Shazam (lyrsync's budget).
- Gemini: use **only Clip Studio's own project key** (`clipstudio-ai`). One project per app is the owner's rule;
  never create extra projects per feature to multiply quota (Google's terms).
- Never print secrets. Owner prefers step-by-step, click-by-click guidance.
- Git: repo `MuhammadSyauqiAlBashir/clipstudio` (private), work on `develop`, commit as
  `-c user.name="Bashir" -c user.email="bashirsyauqi@gmail.com"`. The initial docs were pushed to `develop` on
  2026-10-04 at the owner's request. Ask in the interview whether to use the BashGames flow (push → PR → merge to
  `main`) or the finance flow (push only when asked).
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

## Planned server facts (not created yet)

| Item | Plan |
|---|---|
| Web/API | `clipstudio.service`, user `clipstudio`, 127.0.0.1:**8400** (free port; others use 8000/8090/8100/8200/8210/8300) |
| Worker | `clipstudio-worker.service` (same user; limits above) |
| Data | PocketBase `clip_*`, machine login `svc_clipstudio`, role `clips`; add `clip_*` to the `pb.bashir.my.id` 404 list |
| Files | `/var/lib/clipstudio/{work,clips,models}` (StateDirectory) |
| Secrets | `/etc/clipstudio/env`: `GEMINI_API_KEY` (present), later `GROQ_API_KEY`, `YT_API_KEY`, PB login, `CS_WEBSUB_SECRET` |
| Site | `clips.bashir.my.id` (DNS wildcard already points here); Caddy block `deploy/Caddyfile.clipstudio`; WebSub callback must be public: `/api/websub/callback` |
| Tools to install | ffmpeg (apt), yt-dlp (pip, keep it updated), deno (for YouTube nsig), mediapipe (Python ≤ 3.12 wheels; the server's Python is 3.12) |

## Open owner tasks (remind at session start)

- [ ] Answer the interview questions (DESIGN §11).
- [ ] Create a free **Groq** account (console.groq.com) → API key → put it on the server with a hidden-input command
      (Claude gives it; never paste keys in chat).
- [ ] In Google Cloud project `clipstudio-ai`: enable **YouTube Data API v3** and create an API key (restrict it to that
      API). No billing.
- [ ] Decide the first sources: channel list and/or clipping campaigns (Whop Content Rewards, etc.); and on which
      platforms you'll post (own accounts ready?).
- [ ] Check whether the Biznet plan has a monthly data-transfer limit (each source downloads 0.5–1.5 GB).

## History

- 2026-10-03 — Owner shared the build spec; feasibility + side-hustle verdict (lean version, Rp0, experiment);
  server/free-service impact analysed.
- 2026-10-04 — Separate Gemini projects per app (shop/games/clipstudio keys installed and tested). Repo cloned to
  `~/clipstudio`; research done (open-source clippers, watchers, recorders, detection models, platform rules, money);
  docs written (`CLAUDE.md`, `README.md`, `docs/DESIGN.md`, `docs/RESEARCH.md`, `docs/SPEC-original.md`) and pushed to `develop`.
