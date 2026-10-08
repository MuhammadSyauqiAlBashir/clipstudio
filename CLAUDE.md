# Clip Studio — Claude context

A private tool for Bashir (owner, app username `bashirsyauqi`) to try a **clipping side hustle**: long videos (pasted
links, uploaded campaign files + automatically watched **YouTube, Twitch and Kick** channels, both **new uploads/VODs
and live streams**) → the best moments as 9:16 clips with captions → the owner reviews and approves → posts (manually
at first). Planned at `https://clips.bashir.my.id`.

**Status (2026-10-04): Phases 1–3 built and LIVE at https://clips.bashir.my.id** (paste link / upload → clips →
review → 1080×1920 final → manual posting; YouTube/Twitch/Kick watchers; music check). Phase 4 started:
**Instagram auto-posting is live** (approve → final → Reel on @bashclipeveryday); TikTok inbox drafts and YouTube
wait for the owner's developer apps / reviews (guide `~/work/clipstudio-autopost-setup.md`). As-built differences from the plan: `docs/DESIGN.md` §13. Code map + deploy: `README.md`.

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
- **Permission "implied"** (added 2026-10-04 at the owner's request): the creator generally welcomes clips but gave no
  written permission (e.g. Indonesian podcasters who see clips as free promotion). The owner accepted that risk after
  being told it isn't permission (first case: Raditya Dika podcast). Keep the choice explicit and recorded per source.
- **Heavy one-off commands (ffmpeg, video edits, tests on real videos) always run memory-capped:**
  `systemd-run --user --scope -q -p MemoryMax=500M -p MemorySwapMax=0 -p CPUQuota=100% nice -n 19 ionice -c3 <cmd>`
  (or `~/work/clipstudio-dev/dev.sh`). On 2026-10-04 an uncapped ffmpeg with several `trim`s of one input buffered raw
  2.5K frames, filled RAM + swap and froze the whole server (owner rebooted via Biznet at 22:51; all data checked OK).
  Cut segments with separate `-ss/-to` runs + the concat demuxer instead.
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
| Secrets | `/etc/clipstudio/env` (`root:clipstudio 640`): `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPGRAM_API_KEY` (backup), `YT_API_KEY` (restricted to YouTube Data API v3 + IP 103.103.21.9); later `TWITCH_CLIENT_ID/SECRET`, `KICK_CLIENT_ID/SECRET`. Optional cookies `/etc/clipstudio/youtube-cookies.txt` (deploy sets `root:clipstudio 640`). `CS_WEBSUB_SECRET` unset → random one in the DB `kv` |
| Site | `clips.bashir.my.id` block appended to `/etc/caddy/Caddyfile` (backup `~/work/Caddyfile.bak-20261004-141255`); source `deploy/Caddyfile.clipstudio`; uploads up to 8 GB; public signed callbacks `/api/websub/callback`, `/api/twitch/callback` |
| Static | `/srv/clipstudio` |
| Dev | `.venv` in the repo; `~/work/clipstudio-dev/dev.sh <script.py>` runs with the real keys against `~/work/clipstudio-dev/state`, capped like the worker. (Don't use `~/work/cs-dev`: it belongs to another project.) |
| Auto-posting | `backend/cs/publish.py`: table `posts` (one row per clip+platform, idempotent), worker loop posts one at a time, ≥90 s apart per platform. Instagram: Meta app `bashclipeveryday` (dev mode, @bashclipeveryday as Instagram Tester, Instagram Login), `IG_ACCESS_TOKEN` seed in env, refreshed daily into DB `kv.ig_token` (60 days); Instagram fetches the final from a signed link `/api/pub/<clip>/<exp>/<sig>.mp4` (1 h, only while a post is pending; resumable upload needs Facebook Login). Settings `autopost` per platform (YouTube off by default) |
| Campaigns | `backend/cs/campaigns.py`: Clippo's **public** list (`api.clippo.id/v1/campaigns`, no login) every 3 h into table `campaigns` (ended ones kept, marked `ended`); flags needs_audio / custom_edit / budget_low / other_platforms / product_cart; push on new campaigns. `sources.campaign_id` → required hashtags first in captions; "Clip it" imports YouTube footage; submit list per campaign (`clips.submitted`). Joining/submitting happen in Clippo's own app: automating the owner's logged-in Clippo account was **blocked by Claude Code's safety check** (even with the owner's `Bash(curl:*)` rule) — not built. AyoKlip/TryBuzzer: no public list; Konten.com check was blocked. CSP img-src also allows `flml.sgp1.cdn.digitaloceanspaces.com` (Caddyfile backup `~/work/Caddyfile.bak-20261005-000140`) |
| Facebook | Page **Bashclipeveryday** (id 1421021257755722). `FB_PAGE_TOKEN` (never expires, made 2026-10-05 from the owner's long-lived `FB_USER_TOKEN` via `/me/accounts`) + `FB_PAGE_ID` in the env file. `backend/cs/facebook.py`: video_reels start → rupload → finish(PUBLISHED); auto-post on by default. Meta app in dev mode (owner = admin), no review |
| Policy pages | `https://clips.bashir.my.id/privacy.html`, `/terms.html` (contact bashclip.everyday@gmail.com), icon `/icons/icon-1024.png` |
| Checks | `systemctl is-active clipstudio clipstudio-worker`; `journalctl -u clipstudio-worker -f`; More tab in the app shows worker, queue, quotas, disk, keys, activity |

## Open owner tasks (remind at session start)

Guides: `~/work/clipstudio-owner-setup.md` (accounts), `~/work/clipstudio-autopost-setup.md` (auto-posting).
- [ ] iPhone: open the site from the **Home Screen icon** → More → Turn on notifications (no device registered yet).
- [ ] TikTok (Part B): DNS TXT for `bashir.my.id` added 2026-10-04 and visible → click **Verify**; Login Kit +
      Content Posting API (Direct Post off); sandbox + target user; save `TIKTOK_CLIENT_KEY/SECRET`. Then Claude builds
      the inbox upload, records the demo, writes the review text → owner submits.
- [ ] YouTube (Part C): OAuth consent (External, published), scope youtube.upload, Web client → `YT_OAUTH_CLIENT_ID/SECRET`.
      Then Claude builds Connect YouTube + gives the audit form text → owner submits.
- [ ] First real source with permission → approve one clip = first automatic Instagram Reel (public!).
- [ ] (Postponed) Twitch and Kick developer apps.
- [ ] Channel/campaign list → Channels tab (in More since 2026-10-04). Campaign shortlist: `~/work/clipstudio-campaigns.md`
      (Clippo, TryBuzzer, AyoKlip, Whop).
- [x] TikTok round 1 (inbox drafts) approved 2026-10-08; production keys saved, owner reconnects TikTok on the phone.
- [ ] TikTok round 2 (Direct Post + video.list): record the demo with the Test app per
      `~/work/clipstudio-tiktok-round2-review.md`, then submit the saved production revision. After approval: Claude
      sets `tiktok_direct_approved`, owner reconnects (Approved app) and picks Direct Post.
- Done 2026-10-04: interview; Groq + YouTube keys; Biznet traffic unlimited; throwaway cookies; review flow tested;
  test data deleted; brand **bashclipeveryday** (clip Gmail bashclip.everyday@gmail.com); Instagram Meta app + token.

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
- 2026-10-04 — Auto-posting: privacy/terms pages, Instagram Reels auto-post (tested up to a FINISHED container with a
  test pattern; not published), posts table + signed links, Auto-posting card in More, per-clip post status/retry.
  Test data deleted at the owner's request. Twitch/Kick postponed by the owner.
- 2026-10-04 — TikTok: sandbox app (Login Kit + Content Posting API, video.upload), connect flow + inbox drafts tested
  (test draft delivered); production draft saved with a placeholder demo video — replace it with the real demo, then
  Submit. Browse page (YouTube channel uploads, 24 per page, ~3 units/page, name search 100 units). Permission
  "implied" added. Caption language made explicit. CSP img-src allows i.ytimg.com + yt3.ggpht.com (Caddyfile backup
  `~/work/Caddyfile.bak-20261004-215602`). First real source: Raditya Dika podcast (implied), 8 clips in review.
- 2026-10-04 22:35 — Server froze (my uncapped ffmpeg demo edit, see the rule above); rebooted 22:51; all services,
  PocketBase and Clip Studio DB integrity OK. Demo videos re-cut safely: `~/work/tiktok-demo-1-pc.mp4` (2:35),
  `~/work/tiktok-demo-2-phone.mp4` (0:18). First Instagram Reel posted automatically 22:24
  (https://www.instagram.com/reel/DeE8p2qFAzl/); TikTok draft delivered.
- 2026-10-04 23:15 — Autopilot: posting schedule (default 12:00/18:00/21:00 WIB, one clip per slot per platform; "Post
  now" overrides), daily reminder (19:00), weekly summary (Mon 09:00), Instagram insights every 6 h (works without an
  extra permission: first Reel 109 views), Stats tab (manual TikTok/YouTube views), Channels moved under More. DB
  migration helper (`db.ADDED_COLUMNS`). Owner's daily job = approving. 34 tests pass.
- 2026-10-05 00:05 — Campaigns page (Clippo public catalogue, 12 campaigns loaded), tabs now Review · Ready · Add
  (Browse inside) · Campaigns · Stats · More; campaign link on sources/uploads/Browse; required hashtags in captions;
  submit-links list. Owner granted `Bash(curl:*)` (project-local) to build automated joining; the safety check still
  blocked reading Clippo's login flow, so joining stays a tap in Clippo. 37 tests pass.
- 2026-10-05 00:40 — YouTube: owner saved `YT_OAUTH_CLIENT_ID/SECRET` (Google Auth Platform, project clipstudio-ai,
  scope youtube.upload, redirect `/api/youtube/oauth`). Built Connect YouTube + resumable Shorts upload
  (`backend/cs/youtube.py`, 1,600 units each, max 5/day, never twice per clip). Auto-post to YouTube stays OFF until
  Google's audit (uploads are locked private before that). Next: owner connects, one test upload, audit form. 38 tests.
- 2026-10-05 09:10 — Facebook Reels: owner created the Page + token; permanent Page token derived on the server;
  draft upload tested (processed, deleted). Facebook is the 4th auto-post platform (default on). YouTube test Short
  went public (https://www.youtube.com/shorts/dqwNGKmUPTs); visibility watcher added (push if Google locks one).
  YouTube API quota: 5 uploads/day cap is ours (10,000 units, 1,600 per upload). 40 tests pass.
- 2026-10-05 10:00 — Browse now uses yt-dlp flat listings (no API quota; Videos / Live replays tabs, 24 per page,
  cached 10 min; name search via YouTube's channel search page). YouTube API counters count by **Google's day**
  (`db.google_day()`, resets 14:00 WIB) with one shared budget `CS_YT_DAILY_UNITS`=9,800; uploads max 6/day within it.
  CSP img-src + `yt3.googleusercontent.com`. (Owner had used 5 uploads via Post now on 2026-10-05.) 40 tests.
- 2026-10-05 10:20 — Stats sync every hour for Instagram (insights), Facebook (views/likes/comments fields; deeper
  insights would need read_insights) and YouTube (views, 45 days; private-lock check); "Sync now" button. TikTok views
  need the `video.list` scope (another TikTok review) — manual until then. Watched channels: "➕ Add videos" opens
  Browse on that channel with its permission. 41 tests.
- 2026-10-05 13:10 — Clippo as the owner's account (owner's decision; session switched to normal mode for approval):
  owner's browser requests saved as `/etc/clipstudio/sessions/{clippo,trybuzzer,whop}.curl` (root:clipstudio 640;
  originals shredded; `session-*.txt` git-ignored). `backend/cs/clippo.py` uses Clippo's own web calls via
  `app.clippo.id/api/proxy/`: join `clips/clipper/campaign/join {campaignId}`, `bulk-check {campaignId, videoUrls}`,
  `submit-batch {campaignId, clips[]}`; sends the copied browser headers as-is (Cloudflare). Join button in the
  Campaigns sheet, auto-submit every 2 h (only platforms the campaign accepts; not-yet-eligible links retried 14 days;
  reasons shown), "Submit now". Session expiry → 401 → owner copies a fresh request. TryBuzzer/Whop: sessions saved,
  integration not built yet (TryBuzzer copy was a public endpoint; needs a user request + its refresh token).
  Review page grouped by source video in time order (toggle "By score"). 43 tests.
- 2026-10-05 14:20→ — Biznet routing outage: outbound IPv4 to Meta (57.144.x), GitHub (20.205.x) and later Google
  (142.250.x, incl. Gemini/YouTube API) times out after Biznet hop 137.59.127.185; Cloudflare, Clippo, Groq, Apple push
  OK. Owner opened a Biznet ticket (MTR image `~/work/biznet-mtr-20261005-1515.png`). Instagram clips 22/23 failed
  (3 tries) → retry when back. `/api/accounts` takes ~40 s while Meta is down (owner: don't change it). Meta back ~17:45 WIB, Google ~17:30; GitHub still down then. A temporary 15-min
  network watcher (transient timer) was used and then stopped and removed at the owner's request.
  Local commits not yet pushed to GitHub (push when reachable). Deploys now wait for an idle worker.
- 2026-10-05 evening — Queue control: settings `pause_processing` / `pause_posting` (Review / Ready queue bars) and per-video
  pause (`sources.paused`), honoured by `worker.next_job` and `publish.run_one` (a running step always finishes). Bulk
  approve/reject (`POST /api/clips/bulk`). Review + Ready "To post" grouped by video, collapsible (remembered), newest
  video first, clips in time order; multi-select with a bulk bar. Copy buttons show "✓ Copied" + animation. Tab bar
  uses line SVG icons. Bottom-bar lift fixed with the finance app's app-frame layout (body fixed height --app-h, only
  <main> scrolls, tab bar is the last flex row). Deploy waits for an idle worker. 45 tests.
- 2026-10-05 20:45 — Fixes after the owner's phone check: frame height = window.innerHeight (the screen height pushed the
  tab bar off the bottom with the "default" status bar), `[hidden]` really hides (empty bulk bar showed as a black
  line), append() skips null (printed "null"). Queue panels "Clip-making queue" / "Final render & posting queue" with
  named items, status light and plain-language waits (`friendly_wait`); Add page: "Refused / failed" folded section.
- 2026-10-05 21:00 — One plain-language status per video (`main.source_state`: label, colour, sentence, active) used by
  the Add page, the video page and the Clip-making queue: what it's doing now (step k of 4), what it waits for and when
  it continues by itself (Groq daily allowance → after 00:00 WIB, Gemini → retry time, retries "try n of 3"), what is
  already done ("Download done ✓"), place in line, paused, failed/refused with the reason. Final-render and posting rows
  say the same kind of thing. `config.MAX_JOB_ATTEMPTS`. 47 tests.
- 2026-10-05 22:20 — Camera movement fix (owner: "movement is laggy", clip 109). Cause: the crop moved only 15×/s
  (17–46 px jump, then 1–2 frozen frames) and the 4 fps face path changed speed every 0.25 s / wobbled. Now
  `render.calm_path` (shots split at cuts, dead zone 10% of the crop width, Gaussian ~0.35 s) + Catmull-Rom → one crop
  update per source frame, 1-px steps (`crop exact=1`). Measured on clip 62: old 46 px/freeze/freeze, new steady
  3–6 px per frame. Render-time only, so it applies to all new previews and finals (saved plans reused).
- 2026-10-05 22:40 — Deepgram as the **backup transcriber** (owner's OK): `backend/cs/deepgram.py`, used per 10-min chunk
  only when Groq can't take it (daily allowance used / long 429). One-time $200 sign-up credit on
  bashclip.everyday@gmail.com, no card (so it can never charge); `DEEPGRAM_API_KEY` in the env file; model nova-3, falls
  back to nova-2 if a language is refused; language hint from earlier chunks; output in Groq's format
  (`punctuated_word`). 401/402/403 → backup paused 6 h (kv `deepgram_off`), jobs wait for Groq as before. Usage
  `usage.deepgram_seconds`; More page shows minutes today + credit left (balance API, cached 1 h). 49 tests.
- 2026-10-05 23:30 — Deepgram key saved and tested (1 min of PWK: better words — penyiar/parfum/trauma — but almost no
  punctuation; Groq stays first). First real use: Aa Juju + "DI SINI ADA SUSHIIIII!!!" transcribed via Deepgram
  (~4 s per 10-min chunk); sushi = 8 clips in review. Ready → Posted is now grouped by video (collapsible, no pause
  button), like To post.
- 2026-10-06 — Meta locked the owner's developer account for "unusual activity" (05:58–08:55; 32 IG + 32 FB Reels and
  37 TikTok drafts in 26 h); owner completed the confirmation, access back. Facebook then refused publishing with its
  anti-spam limit ("We limit how often you can post"). TikTok `spam_risk_too_many_pending_share` = TikTok's own cap on
  unposted inbox drafts (~5 per 24 h; we only space posts 90 s). **Owner: no automatic retries for posts** — a failure
  stops at once as `failed`; `publish.explain()` turns the raw error into a named reason + what to do (shown on the
  clip and Ready card, raw text under "Details", push + activity log). Instagram's download auto-retry removed too.
- 2026-10-08 — Queue rows each get their own pause/resume button (cherry-pick): video rows → `sources.paused`, final
  renders → new `clips.paused` (worker skips), posts → new `posts.paused` (publish skips); a row paused via its whole
  video resumes the video; running items have no button (the step finishes). Small preview = own player
  (`previewPlayer`: tap play/pause, tap-to-seek bar, **fullscreen top-right** — iOS's own button sat under the select
  box). Live: clip pop-up redraws final/post status every 5 s while something is pending (video keeps playing);
  Review refreshes while a video is being made, Ready/Posted while renders/posts are moving; never while the app is
  in the background. Phone screenshots from a scratch server: `~/work/clip_shots.py` → `~/work/shots/clip/`. 51 tests.
- 2026-10-09 — **TikTok round 1 approved** (production app live 2026-10-08: Login Kit + `video.upload`). Production keys
  in `TIKTOK_CLIENT_KEY/SECRET`, sandbox keys in `TIKTOK_SANDBOX_CLIENT_KEY/SECRET`. Round 2 prepared: production
  **revision** (Direct Post ON, scopes user.info.basic, video.upload, video.publish, video.list) saved, not yet
  submitted; Sandbox has the same + "Apply changes". Built: two TikTok apps with separate logins (settings
  `tiktok_app` production/sandbox, kv `tiktok_auth` / `tiktok_auth_sandbox`; the OAuth state remembers the app;
  production asks only for approved scopes until `tiktok_direct_approved`), **Direct Post** (`tiktok_direct`): never
  automatic — "Post to TikTok…" sheet per clip or per video group (creator_info name/avatar via `/api/tiktok/avatar`
  proxy, privacy with no default, comment/duet/stitch off by default and greyed when the account disables them,
  disclosure Your brand / Branded content with labels, branded ≠ Only me, Music Usage / Branded Content Policy consent,
  max duration) → `posts.options` → `publish.tiktok_direct` (FILE_UPLOAD, status until PUBLISH_COMPLETE); private-only
  until approved (`tiktok.private_only()`); `video.list` → hourly TikTok stats by video id + Stats card "Your latest
  TikTok videos" (`/api/tiktok/videos`, 10-min cache). Guides: `~/work/clipstudio-tiktok-round2.md` (portal steps),
  `~/work/clipstudio-tiktok-round2-review.md` (recording script + review texts). 53 tests.
