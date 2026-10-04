# Clip Studio — design (2026-10-04, interview answers included)

Turns long videos (pasted links, uploaded campaign files, and automatically watched **YouTube, Twitch and Kick**
channels: **new uploads/VODs and live streams**) into vertical 9:16 clips with captions, queues them for the owner's
review, and (later) publishes approved clips.
Owner's original spec: [`SPEC-original.md`](SPEC-original.md). Evidence behind every choice: [`RESEARCH.md`](RESEARCH.md).

**Status: Phases 1–3 built and deployed 2026-10-04 (https://clips.bashir.my.id). Phase 4 (publishing APIs) not
built. What the build changed from this plan: §13.**

## 0. Why the plan differs from the original spec

| Spec says | Plan | Why |
|---|---|---|
| Self-hosted Whisper | **Groq Whisper API (free)**, faster-whisper `base`/`small` only as an offline fallback | 2 vCPU / 2 GB can't transcribe hours of audio in reasonable time; Groq free = 8 h audio/day with word timestamps |
| Build on OpenShorts / SupoClip | **Own lean pipeline** borrowing their ideas | OpenShorts wants 8 GB+ RAM + Docker; SupoClip needs Postgres + Redis + Docker and is AGPL |
| Poll YouTube Data API for uploads/live | **WebSub push + RSS safety poll + `videos.list` (1 unit)** | No quota for detection; never `search.list` (100 units) |
| Twitch/Kick APIs | **Twitch EventSub webhooks + Helix poll; Kick official API webhook + poll.** All three platforms equal (owner, 2026-10-04) | Free; Kick downloads are fragile (Cloudflare) — failures are logged, **no Cloudflare bypass** |
| Publish to 3 platforms via APIs on approval | **v1: manual posting** (download + copy caption); later TikTok drafts; full auto only after platform audits | Unaudited YouTube/TikTok API uploads are locked private |
| Build order: publisher 2nd, scorer 3rd, watchers last | **Watchers 2nd, scorer 3rd, publishing 4th**; the music check moves into Phase 1 | Owner asked for watchers; publishing stays manual until the audits pass anyway; "music excluded" is a hard rule |
| Local or paid LLM | **Gemini free tier, Clip Studio's own project** (`clipstudio-ai`) | Separate quota from finance/shop/games; no cost |
| Laughter + music models | **Loudness from FFmpeg (free) + Gemini audio check on candidate segments**; inaSpeechSegmenter optional | TensorFlow/PyTorch models are heavy for 2 GB; candidate-only checks are cheap |
| Render every clip in final quality | **Low-quality preview (540×960) for review; final 1080×1920 rendered only after approval** (owner's idea) | Most CPU goes only into clips that will be posted; edits are baked into the final |
| — | **Originality step** (hook title, creator credit, optional owner text on the clip) | YouTube cut reach for unoriginal reposted Shorts on 2026-10-01 |

## 1. Goals and principles

- A **side-hustle experiment**: cheap (Rp0 extra), quick to try, measurable in ~1 month (views per clip, campaign payouts).
- **Permission-based sources only** (clip_permission gate). No movie/TV/studio/sports. Music segments excluded.
- **Nothing publishes without the owner's approval.** Credit the original creator.
- **Never hurt the live apps** (finance holds real money data): hard CPU/RAM caps, one processing job at a time, low priority.
- Same house style as the other apps: FastAPI + PocketBase + static PWA (plain ES modules), sandboxed systemd, secrets
  in `/etc/clipstudio/env`, private login via the shared `users` + approval (users `bashirsyauqi` and `bells`).

## 2. Architecture

```
YouTube hub (WebSub) ──POST──▶ /api/websub/callback ──┐
YouTube RSS poll (10–15 min) ─────────────────────────┤
Twitch EventSub ──POST──▶ /api/twitch/callback ───────┤
Twitch Helix /streams poll (5 min) ───────────────────┼─▶ watcher: new video / went live → details
Kick webhook ──POST──▶ /api/kick/callback ────────────┤     (YT videos.list 1 unit / Helix / Kick API)
Kick API poll (5 min) ────────────────────────────────┤     upload? live now? upcoming? duration? → gate → job
Owner pastes URL / uploads a campaign file ───────────┘
                                                                         │
                                       job queue (PocketBase clip_jobs, manual first, 1 processing job at a time)
                                                                         ▼
  download (yt-dlp, ≤1080p) / record live (yt-dlp --live-from-start or streamlink; 1 recording at a time,
  may run beside the processing job) → /var/lib/clipstudio/work
  → audio 16 kHz mono Opus → Groq Whisper (chunks, word timestamps) → transcript
  → signals: loudness curve (FFmpeg ebur128) + Gemini on transcript (word-index moments + hook titles)
  → candidates → Gemini audio check per candidate (music → excluded; laughter → boost) → composite score → top N
  → MediaPipe faces (4 fps) → smoothed crop path (stored) → PREVIEW render 540×960 + .ass karaoke captions
  → push "N clips ready"
                                                                         ▼
  Review PWA (clips.bashir.my.id, phone first): watch preview · approve / reject (reason) · edit caption/hook/own text
  → on approve: FINAL render 1080×1920 from the source with the stored crop path + edits → download + copy caption
  → (later) publisher: TikTok draft upload, YouTube/Instagram after audits
```

Units: `clipstudio.service` (web/API, user `clipstudio`, 127.0.0.1:**8400**) and `clipstudio-worker.service`
(pipeline; `Nice=10`, `CPUQuota=100%` (= 1 of 2 cores), `MemoryMax=900M`, `IOSchedulingClass=idle`). The recorder runs
inside the worker as a separate process with its own limits (mostly network + disk, little CPU).

## 3. Data model (as planned; **built as Clip Studio's own SQLite DB**, tables without the `clip_` prefix — see §13)

| Collection | Key fields |
|---|---|
| `clip_channels` | platform (**youtube / twitch / kick**), platform channel id (YouTube `UC…`, Twitch user id, Kick broadcaster id), handle, title, **clip_permission** (explicit / platform-default / campaign / **blocked** = default), campaign (name, rate per 1k, rules URL), watch_uploads, watch_live, min_duration, subscription state (WebSub lease / EventSub id / Kick subscription), notes |
| `clip_sources` | channel, platform, url, video_id, kind (vod/live/replay/manual/upload), title, duration, published_at, permission_snapshot, status (queued/downloading/recording/transcribing/scoring/previewing/review/done/failed/rejected_by_gate), reason, files, sizes, **keep_until** (source deleted when all clips are decided or after 7 days) |
| `clip_jobs` | source or clip, kind (process / final_render), priority (manual first, final renders before new sources), step, progress, attempts, log, started/finished |
| `clip_transcripts` | source, language, words JSON (word,start,end) stored as a file, provider (groq/local) |
| `clip_candidates` | source, word_from/word_to, start/end, signals (loudness, laughter, music, llm_reason, llm_score), composite score, hook title |
| `clip_clips` | candidate, **preview file**, **final file**, thumb, duration, crop path (JSON file), caption text, hook title, **owner_text**, hashtags, creator credit, status (review/approved/rendering/ready/rejected/expired/posted), reject_reason, posted_urls per platform, views (manual entry) |
| `clip_events` | gate rejections, errors, quota usage (Groq seconds, Gemini calls, YouTube units) per day |
| `clip_kv` | settings (thresholds, caption style, max clips per source, daily caps), Web Push subscriptions |

## 4. Pipeline details

1. **Gate** (before anything is downloaded): the channel must have `clip_permission != blocked`; duration limits;
   title/description keyword blocklist (e.g. "full movie", "episode", league names) → logged reason. Manual URLs and
   uploads need a permission choice from the owner on submit.
2. **Download/record**: yt-dlp (latest; deno for YouTube nsig; optional PO-token provider). Source quality **up to
   1080p** (final output is 1080×1920). YouTube cookies: only from **one throwaway account**, only when blocked; if it
   gets banned, stop and ask the owner (no automatic new accounts). Twitch: yt-dlp/streamlink (VODs kept 7–60 days).
   Kick: normal yt-dlp/streamlink only — if Cloudflare blocks, the source fails with the reason (no bypass).
   Live: start when the watcher sees "live", record from start where possible, stop on end or after **4 h max**, then
   process like a VOD. **One recording at a time**; if a second watched channel goes live, queue its **replay/VOD**
   after it ends. Disk guard: refuse if < 10 GB free.
3. **Audio + transcription**: FFmpeg → 16 kHz mono Opus ~32 kbps; split into ≤ 10-min chunks with 2 s overlap; Groq
   `whisper-large-v3-turbo`, `verbose_json` + word timestamps; merge; language auto (Indonesian/English; captions in the
   spoken language). Fallback: local faster-whisper `base` int8 (slow) if Groq is down; track daily audio seconds
   (cap below 28,800 = 8 h/day across all platforms).
4. **Candidates**:
   - Loudness: FFmpeg `ebur128` short-term loudness every 0.5 s → z-score vs a rolling 60 s baseline → spikes.
   - Gemini (own key) on the transcript in windows (~10–15 min each with overlap): return moments as **word index
     ranges** + reason + score + a short hook title (same language as the speech); complete thoughts (setup → payoff), 15–60 s.
   - Snap cuts to sentence ends / pauses > 0.3 s; merge overlaps.
5. **Checks per candidate** (cheap, **Phase 1**): cut its audio, ask Gemini (audio input) "music in background?
   recognisable song? laughter? crowd reaction?" → **exclude music**; boost laughter/reaction (boost tuned in Phase 3).
6. **Score**: composite = LLM score + loudness boost + laughter boost; music → excluded → **up to 8 per hour of
   source**, 15–60 s, above a threshold.
7. **Preview render**: sample frames at 4 fps → MediaPipe face boxes → layout (TRACK one face / SPLIT two faces /
   GENERAL blurred background when no face) → smoothed crop path (no jumps; respect shot cuts), **saved per clip** →
   FFmpeg `ultrafast` **540×960**, burned `.ass` **karaoke captions** (3–5 words, active word highlighted), hook title
   for the first 3 s, small "🎥 @creator" credit → thumbnail.
8. **Notify + review**: Web Push (VAPID, like finance/shop/games; iPhone needs the home-screen install, iOS 16.4+,
   `Urgency: high`). Review page (phone first) lists clips with score + signals; approve / reject (reason) / edit
   caption, hook title, optional **owner text** overlay.
9. **Final render (on approve)**: from the kept source, reuse the stored crop path + edits → FFmpeg `libx264` (`veryfast`
   or `faster`) **1080×1920**, source frame rate up to 60 → download button + "copy caption" (template per platform +
   hashtags + credit). Final renders jump ahead of new sources in the queue.
10. **Cleanup**: delete the source video when every clip from it is approved/rejected, or after **7 days** (undecided
    clips → `expired`); delete clip files 30 days after posted/rejected; keep transcripts.

## 5. Watchers (uploads/VODs + live)

**YouTube**
- On adding a channel: resolve handle → channel id (yt-dlp metadata or one `channels.list` call, 1 unit), subscribe via
  WebSub (callback `https://clips.bashir.my.id/api/websub/callback`, verify token + `hub.secret` HMAC, lease ~5 days,
  renew daily).
- Callback: verify `hub.challenge`; on POST check `X-Hub-Signature`, parse the Atom entry → video id → dedupe (title
  edits also notify) → `videos.list?part=snippet,contentDetails,liveStreamingDetails` (1 unit for up to 50 ids).
- RSS poll every 10–15 min per channel (catches missed pushes; free).
- Upcoming live (scheduled): store `scheduledStartTime`; re-check a few minutes before; when live → record.
- Quota: 10,000 units/day free → we need maybe 50–300/day. Key `YT_API_KEY` (project `clipstudio-ai`, restricted to
  YouTube Data API v3 + the server IP) — installed and tested 2026-10-04.

**Twitch**
- Free developer app (dev.twitch.tv; Twitch account with 2FA) → `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`, app access token.
- EventSub **webhook** subscriptions `stream.online` / `stream.offline` per channel (cost 1 each, 10,000 max) →
  callback `https://clips.bashir.my.id/api/twitch/callback` (verify the HMAC signature; answer the verification challenge;
  handle revocation).
- Safety poll: Helix `GET /streams?user_id=…` (up to 100 ids per call) every ~5 min; new VODs via `GET /videos?user_id=…&type=archive`.

**Kick** (best effort)
- Free developer app on kick.com → `KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`; webhook `livestream.status.updated` to
  `https://clips.bashir.my.id/api/kick/callback` (verify the signature) + an official-API poll every ~5 min.
- Downloads use normal tools; expect breakage (Cloudflare, URL changes). Failures are logged and shown, never bypassed.

**Concurrency**: one live recording at a time (beside one processing job); a second simultaneous live → queue its
replay/VOD after it ends.

## 6. Server budget (measured 2026-10-03)

| | Today | + Clip Studio idle | + processing |
|---|---|---|---|
| RAM (apps) | ~350 MB | ~420 MB | ~1.0–1.1 GB peak (MediaPipe + FFmpeg ~500–700 MB) |
| CPU | ~10% | ~10% | worker capped at 1 of 2 cores; previews are cheap, 1080p renders only for approved clips |
| Disk | 47 GB free | — | 1080p source ~1.5–3 GB/h, kept ≤ 7 days; previews ~3–5 MB, finals ~20–40 MB each; live ~2–3 GB/h at 1080p |

Rules: one processing job at a time (+ one recording); low priority; refuse new downloads when free disk < 10 GB;
heavy jobs ideally when no Claude/VS Code session is open (each Claude session ~220–370 MB, VS Code remote ~300 MB).
Network: ~1.5–3 GB download per hour of 1080p source; Biznet NEO Lite traffic is **unlimited, no quota** (checked
2026-10-04 on biznetgio.com), so only disk and time limit how much we download.
No server upgrade needed for v1.

## 7. Free services and quotas

| Service | Use | Free limit | Our guard |
|---|---|---|---|
| Groq Whisper (owner's account, free, no card; key `GROQ_API_KEY` installed 2026-10-04) | transcription | 8 h audio/day, 2,000 req/day, 20 req/min | count audio seconds/day; queue waits for the reset |
| Gemini (`clipstudio-ai` project, key in `/etc/clipstudio/env`) | moments, hook titles, audio checks | per-model daily limits, reset 14:00 WIB | windows + candidate-only audio; fallback across models; never billing |
| YouTube Data API (same Google project) | `videos.list`, `channels.list` | 10,000 units/day | only 1-unit calls; log units |
| WebSub hub + RSS | YouTube upload/live detection | free | dedupe, lease renewal |
| Twitch API (EventSub + Helix) | Twitch live/VOD detection | free; EventSub max total cost 10,000 | 2 subscriptions per channel |
| Kick public API | Kick live detection | free | webhook + slow poll |
| Cloudflare Workers AI, Shazam | **not used** (protect the shop's FLUX budget and lyrsync's Shazam budget) | — | — |

The Google Cloud project `clipstudio-ai` lives on the owner's **finance Google account** (not bashirsyauqi@gmail.com).

## 8. Publishing roadmap

Target platforms: **TikTok, YouTube Shorts, Instagram Reels**, on **new accounts made just for clips** (owner, 2026-10-04).

1. **v1 manual:** download the approved 1080×1920 clip + copy the ready caption (with creator credit and hashtags);
   post from the phone.
2. TikTok **Upload to inbox** (drafts; allowed without audit) — the owner finishes the post in the TikTok app.
3. YouTube / Instagram / TikTok direct post only after each platform's app audit (YouTube compliance audit; TikTok
   Content Posting audit; Instagram Professional account + Facebook Page + Meta app). One approval → staggered
   uploads, idempotent (no duplicate posts on retry).

## 9. Copyright, originality, platform health

- clip_permission gate (default blocked), hard rejects (movies/TV/studio/sports), music excluded, creator credit.
- **Originality (YouTube 2026-10-01 change):** auto hook title + creator credit on every clip; the owner can add his
  own text on a clip during review (Phase 1); consistent channel branding, no mass duplicates; prefer campaigns that
  provide authorised footage and pay per view.
- Respect platform rate limits; stagger posts; keep the owner in the loop.

## 10. Build phases

1. **Phase 1 – prove clip quality:** paste a URL (YouTube / Twitch / Kick) or upload a campaign file → download →
   Groq transcript → Gemini moments + loudness → **Gemini music check (exclude)** → preview 540×960 + karaoke captions →
   review site (login `bashirsyauqi` + `bells`; approve/reject/edit caption, hook, own text) → **final 1080×1920 on
   approve** → download + copy caption. Web Push when clips are ready.
2. **Phase 2 – watchers:** channel list + gate UI; YouTube (WebSub + RSS + `videos.list`), Twitch (EventSub + Helix),
   Kick (webhook + poll); auto-download new uploads/VODs; live recording (1 at a time, 4 h max, replay for the second).
3. **Phase 3 – smarter scoring:** laughter/reaction boosts tuned, optional inaSpeechSegmenter, thresholds tuned from
   the owner's approvals/rejections, per-campaign rules.
4. **Phase 4 – publishing:** TikTok drafts, then audits for direct posting; stats (views entered or fetched).

## 11. Interview answers (owner, 2026-10-04)

| # | Question | Answer |
|---|---|---|
| 1 | First sources | YouTube channels **and** clipping campaigns, plus Twitch (and Kick) — specific channels/campaigns still to be listed |
| 2 | Platforms | TikTok, YouTube Shorts, Instagram Reels — **new accounts**, not made yet |
| 3 | Twitch/Kick | All three platforms (YouTube, Twitch, Kick) **equally**; Kick accepted as best effort, no Cloudflare bypass |
| 4 | Language | Auto (Indonesian or English, captions in the spoken language) |
| 5 | Caption style | Karaoke highlight (3–5 words, active word lit) |
| 6 | Quality | Review previews in low quality; **final 1080×1920 rendered after approval** (owner's idea) |
| 7 | Clips per source | Up to 8 per hour of video, 15–60 s |
| 8 | Live | Max 4 h per recording; 1 recording at a time; a second live → download its replay afterwards |
| 9 | YouTube cookies | OK with a throwaway account ("ok to get banned"). Rule: one throwaway, never mixed with real accounts; if banned, stop and ask (repeated new accounts break Google's terms and could risk the real accounts) |
| 10 | Originality | Auto hook title + credit; owner can add his own text on a clip |
| 11 | Notifications / review | Web Push; review on the phone |
| 12 | Login | `clips.bashir.my.id`; `bashirsyauqi` and `bells` |
| 13 | Git | BashGames flow: work on `develop`, push → PR → merge to `main` |

Still open: the actual channel list / campaigns; Twitch + Kick developer apps (needed
for Phase 2); the clip accounts on TikTok/YouTube/Instagram (needed before posting).

## 12. Risks

- YouTube blocking downloads from the VPS IP (most likely failure; needs maintenance).
- Kick downloads breaking (Cloudflare, site changes) — best effort only.
- Platform rule changes (originality, API audits); campaign rules differ per campaign.
- Free-tier changes (Groq/Gemini/Twitch/Kick limits).
- Income uncertainty: average clipper earnings are low; consistency matters more than tooling.
- Resource contention with live apps → enforced by systemd limits; 1080p sources use more disk and bandwidth.

## 13. As built (2026-10-04) — differences from the plan above

| Plan | Built | Why |
|---|---|---|
| PocketBase `clip_*` collections + machine login | **Own SQLite** `/var/lib/clipstudio/clipstudio.db` (WAL), shared by web + worker; login still via the shared `users` but **read-only** (password check/refresh), allowlist `bashirsyauqi`, `bells` | Owner: "use a separate database if you want; don't touch other data". Nothing in PocketBase changes; the worker's frequent writes stay out of the shared DB. Not in the nightly PocketBase backup (clips/transcripts can be remade) |
| MediaPipe faces | **OpenCV YuNet** (`FaceDetectorYN`, MIT, 230 KB model in `backend/cs/assets/`) | Same job, far lighter install (no jax/matplotlib) |
| streamlink for live | **yt-dlp only** (`--live-from-start` on YouTube, MPEG-TS, SIGINT at 4 h) | One tool to keep updated |
| Kick webhook | **Kick API poll every 3 min**, live only (the API has no VOD list; a missed Kick live has no replay) | Fewer moving parts; webhook can be added later |
| Candidates table | Candidates and clips are one `clips` table (`status` candidate/excluded/review/approved/rendering/ready/rejected/expired/posted) | Simpler; excluded moments stay visible with the reason |
| Edits re-render the preview | Edits go into the **final** only (preview keeps the old text; editing a ready clip re-queues its final) | Saves CPU |

Measured on the server (30-min CC-BY interview): whole pipeline ≈ 6 min (Groq ~1 min, Gemini incl. 503 fallbacks ~2
min, 5 previews ~20 s each); final 1080×1920 of a 37 s clip ≈ 70 s on one core (6.5 MB); worker peak RAM ≈ 690 MB.
