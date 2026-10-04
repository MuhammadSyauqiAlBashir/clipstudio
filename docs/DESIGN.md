# Clip Studio — design (draft, 2026-10-04)

Turns long videos (pasted links + automatically watched YouTube channels: **new uploads and live streams**) into
vertical 9:16 clips with captions, queues them for the owner's review, and (later) publishes approved clips.
Owner's original spec: [`SPEC-original.md`](SPEC-original.md). Evidence behind every choice: [`RESEARCH.md`](RESEARCH.md).

**Status: not built. Docs only. Next step = short owner interview (§11), then Phase 1.**

## 0. Why the plan differs from the original spec

| Spec says | Plan | Why |
|---|---|---|
| Self-hosted Whisper | **Groq Whisper API (free)**, faster-whisper `base`/`small` only as an offline fallback | 2 vCPU / 2 GB can't transcribe hours of audio in reasonable time; Groq free = 8 h audio/day with word timestamps |
| Build on OpenShorts / SupoClip | **Own lean pipeline** borrowing their ideas | OpenShorts wants 8 GB+ RAM + Docker; SupoClip needs Postgres + Redis + Docker and is AGPL |
| Poll YouTube Data API for uploads/live | **WebSub push + RSS safety poll + `videos.list` (1 unit)** | No quota for detection; never `search.list` (100 units) |
| Publish to 3 platforms via APIs on approval | **v1: manual posting** (download + copy caption); later TikTok drafts; full auto only after platform audits | Unaudited YouTube/TikTok API uploads are locked private |
| Local or paid LLM | **Gemini free tier, Clip Studio's own project** (`clipstudio-ai`) | Separate quota from finance/shop/games; no cost |
| Laughter + music models | **Loudness from FFmpeg (free) + Gemini audio check on candidate segments**; inaSpeechSegmenter optional | TensorFlow/PyTorch models are heavy for 2 GB; candidate-only checks are cheap |
| — | **Originality step** (hook title, optional commentary text, creator credit) | YouTube cut reach for unoriginal reposted Shorts on 2026-10-01 |

## 1. Goals and principles

- A **side-hustle experiment**: cheap (Rp0 extra), quick to try, measurable in ~1 month (views per clip, campaign payouts).
- **Permission-based sources only** (clip_permission gate). No movie/TV/studio/sports. Music segments excluded.
- **Nothing publishes without the owner's approval.** Credit the original creator.
- **Never hurt the live apps** (finance holds real money data): hard CPU/RAM caps, one job at a time, low priority.
- Same house style as the other apps: FastAPI + PocketBase + static PWA (plain ES modules), sandboxed systemd, secrets
  in `/etc/clipstudio/env`, private login via the shared `users` + approval.

## 2. Architecture

```
YouTube hub (WebSub) ──POST──▶ /api/websub/callback ─┐
RSS poll (10–15 min) ────────────────────────────────┼─▶ watcher: new video id → videos.list (1 unit):
Owner pastes URL on the site ────────────────────────┘     upload? live now? upcoming? duration? → gate → job
                                                                         │
                                                job queue (PocketBase clip_jobs, manual first, 1 at a time)
                                                                         ▼
  download (yt-dlp) / record live (ytarchive-style yt-dlp --live-from-start, or streamlink) → /var/lib/clipstudio/work
  → audio 16 kHz mono Opus → Groq Whisper (chunks, word timestamps) → transcript
  → signals: loudness curve (FFmpeg ebur128) + Gemini on transcript (word-index moments + hook titles)
  → candidates → Gemini audio check per candidate (music? laughter?) → composite score → top N
  → render each: MediaPipe faces (4 fps) → smoothed crop path → FFmpeg cut + 9:16 + .ass karaoke captions
  → clips in /var/lib/clipstudio/clips + thumbnails → push "N clips ready"
                                                                         ▼
  Review PWA (clips.bashir.my.id): watch · approve / reject (reason) · edit caption/hook → download + copy caption
  → (later) publisher: TikTok draft upload, YouTube/Instagram after audits
```

Units (proposal): `clipstudio.service` (web/API, user `clipstudio`, 127.0.0.1:**8400**) and
`clipstudio-worker.service` (pipeline; `Nice=10`, `CPUQuota=100%` (= 1 of 2 cores), `MemoryMax=900M`,
`IOSchedulingClass=idle`). Recorder runs inside the worker as a separate process with its own limits.

## 3. Data model (PocketBase, prefix `clip_`, machine login `svc_clipstudio` role `clips`)

| Collection | Key fields |
|---|---|
| `clip_channels` | platform (youtube), channel_id (UC…), handle, title, **clip_permission** (explicit / platform-default / campaign / **blocked** = default), campaign (name, rate per 1k, rules URL), watch_uploads, watch_live, min_duration, websub_lease_until, notes |
| `clip_sources` | channel, url, video_id, kind (vod/live/manual), title, duration, published_at, permission_snapshot, status (queued/downloading/recording/transcribing/scoring/rendering/done/failed/rejected_by_gate), reason, files, sizes |
| `clip_jobs` | source, priority (manual first), step, progress, attempts, log, started/finished |
| `clip_transcripts` | source, language, words JSON (word,start,end) stored as a file, provider (groq/local) |
| `clip_candidates` | source, word_from/word_to, start/end, signals (loudness, laughter, music, llm_reason, llm_score), composite score, hook title |
| `clip_clips` | candidate, file, thumb, duration, caption text, hashtags, creator credit, status (review/approved/rejected/posted), reject_reason, posted_urls per platform, views (manual entry) |
| `clip_events` | gate rejections, errors, quota usage (Groq seconds, Gemini calls, YouTube units) per day |
| `clip_kv` | settings (thresholds, caption style, max clips per source, daily caps) |

## 4. Pipeline details

1. **Gate** (before anything is downloaded): the channel must have `clip_permission != blocked`; duration limits;
   title/description keyword blocklist (e.g. "full movie", "episode", league names) → logged reason. Manual URLs
   need a permission choice from the owner on submit.
2. **Download/record**: yt-dlp (latest; deno for YouTube nsig; optional PO-token provider; cookies from a throwaway
   account only if the owner agrees). 720p is enough (we output 720×1280 or 1080×1920). Live: start when the watcher
   sees `liveBroadcastContent=live`, record from start (`--live-from-start`), stop on end/after a max length
   (e.g. 4 h), then process like a VOD. One recording at a time; disk guard (refuse if < 10 GB free).
3. **Audio + transcription**: FFmpeg → 16 kHz mono Opus ~32 kbps; split into ≤ 10-min chunks with 2 s overlap; Groq
   `whisper-large-v3-turbo`, `verbose_json` + word timestamps; merge; language auto (Indonesian/English). Fallback:
   local faster-whisper `base` int8 (slow) if Groq is down; track daily audio seconds (cap below 28,800).
4. **Candidates**:
   - Loudness: FFmpeg `ebur128` short-term loudness every 0.5 s → z-score vs a rolling 60 s baseline → spikes.
   - Gemini (own key) on the transcript in windows (~10–15 min each with overlap): return moments as **word index
     ranges** + reason + score + a short hook title; ask for complete thoughts (setup → payoff), 15–60 s.
   - Snap cuts to sentence ends / pauses > 0.3 s; merge overlaps.
5. **Checks per candidate** (cheap): cut its audio, ask Gemini (audio input) "music in background? recognisable
   song? laughter? crowd reaction?" → exclude music; boost laughter/reaction. Optional local inaSpeechSegmenter.
6. **Score**: composite = LLM score + loudness boost + laughter boost − music penalty (excluded if music) → top N
   (e.g. 8 per hour of source) above a threshold.
7. **Render**: sample frames at 4 fps → MediaPipe face boxes → choose layout (TRACK one face / SPLIT two faces /
   GENERAL blurred background when no face) → smooth the crop path (no jumps; respect shot cuts) → FFmpeg
   `libx264 veryfast`, 720×1280 (1080×1920 optional), burned `.ass` karaoke captions (3–5 words, active word
   highlighted), optional hook title for the first 3 s, small "🎥 @creator" credit → thumbnail.
8. **Notify + review**: push to the owner; review page lists clips with score + signals; approve/reject/edit; download
   button + "copy caption" (caption template per platform + hashtags + credit).
9. **Cleanup**: delete the source video after rendering; delete clips 30 days after posted/rejected; keep transcripts.

## 5. Watchers (YouTube uploads + live)

- On adding a channel: resolve handle → channel id (yt-dlp metadata or one `channels.list` call, 1 unit), subscribe via
  WebSub (callback `https://clips.bashir.my.id/api/websub/callback`, verify token, lease ~5 days, renew daily).
- Callback: verify `hub.challenge`; on POST parse the Atom entry → video id → dedupe (title edits also notify) →
  `videos.list?part=snippet,contentDetails,liveStreamingDetails` (1 unit for up to 50 ids).
- RSS poll every 10–15 min per channel (catches missed pushes; free).
- Upcoming live (scheduled): store `scheduledStartTime`; re-check a few minutes before; when live → record.
- Quota: YouTube Data API 10,000 units/day free → we need maybe 50–300/day. Needs a YouTube Data API key: enable
  the API in the `clipstudio-ai` Google Cloud project (same project, separate key/restriction).

## 6. Server budget (measured 2026-10-03)

| | Today | + Clip Studio idle | + rendering |
|---|---|---|---|
| RAM (apps) | ~350 MB | ~420 MB | ~1.0–1.1 GB peak (MediaPipe + FFmpeg ~500–700 MB) |
| CPU | ~10% | ~10% | worker capped at 1 of 2 cores |
| Disk | 47 GB free | — | per 1 h source: 0.5–1.5 GB temporary; clips ~10–20 MB each; live: ~1–1.5 GB/h at 720p |

Rules: one job at a time; low priority; refuse new downloads when free disk < 10 GB; heavy jobs ideally when no
Claude/VS Code session is open (they use ~900 MB). Network: ~0.5–1.5 GB download per source; check the Biznet plan's
transfer allowance. No server upgrade needed for v1.

## 7. Free services and quotas

| Service | Use | Free limit | Our guard |
|---|---|---|---|
| Groq Whisper (owner signs up, free, no card) | transcription | 8 h audio/day, 2,000 req/day, 20 req/min | count audio seconds/day; queue waits for the reset |
| Gemini (`clipstudio-ai` project, key in `/etc/clipstudio/env`) | moments, hook titles, audio checks | per-model daily limits, reset 14:00 WIB | windows + candidate-only audio; fallback across models; never billing |
| YouTube Data API (same Google project) | `videos.list`, `channels.list` | 10,000 units/day | only 1-unit calls; log units |
| WebSub hub + RSS | upload/live detection | free | dedupe, lease renewal |
| Cloudflare Workers AI, Shazam | **not used** (protect the shop's FLUX budget and lyrsync's Shazam budget) | — | — |

## 8. Publishing roadmap

1. **v1 manual:** download the approved clip + copy the ready caption (with creator credit and hashtags); post from the phone.
2. TikTok **Upload to inbox** (drafts; allowed without audit) — the owner finishes the post in the TikTok app.
3. YouTube / Instagram / TikTok direct post only after each platform's app audit (YouTube compliance audit; TikTok
   Content Posting audit; Instagram Professional account + Facebook Page + Meta app). One approval → staggered
   uploads, idempotent (no duplicate posts on retry).

## 9. Copyright, originality, platform health

- clip_permission gate (default blocked), hard rejects (movies/TV/studio/sports), music excluded, creator credit.
- **Originality (YouTube 2026-10-01 change):** add value per clip: hook title, optional owner commentary text/voice
  (later), consistent channel branding, no mass duplicates; prefer campaigns that provide authorised footage and
  pay per view.
- Respect platform rate limits; stagger posts; keep the owner in the loop.

## 10. Build phases (adapted from the spec)

1. **Phase 1 – prove clip quality:** manual URL (and file upload for campaign footage) → download → Groq transcript →
   Gemini moments + loudness → render 9:16 + captions → review site (approve/reject/edit, download + copy caption).
2. **Phase 2 – watchers:** channel list + gate UI, WebSub + RSS + `videos.list`, auto-download new uploads, live
   recording (one at a time), push notifications.
3. **Phase 3 – smarter scoring:** Gemini audio checks (music/laughter), optional inaSpeechSegmenter, thresholds tuned
   from the owner's approvals/rejections, per-campaign rules.
4. **Phase 4 – publishing:** TikTok drafts, then audits for direct posting; stats (views entered or fetched).

## 11. Questions for the owner (interview before Phase 1)

1. Which sources first: specific YouTube channels (names/links)? Do you already have clipping campaigns (Whop etc.)
   with their footage, or your own content?
2. Which platforms will you post on first (TikTok / YouTube Shorts / Instagram)? Own accounts already made?
3. Clip language and caption language (Indonesian, English, both)? Caption style (karaoke highlight / one word at a
   time / clean)? Output 720×1280 (faster) or 1080×1920?
4. Clips per source and length range (default 15–60 s, up to 8 per hour of video)?
5. Live streams: record whole streams (up to how many hours?) or only the last N hours? How many channels at once?
6. YouTube downloads from the server may get blocked: OK to use cookies from a **throwaway** YouTube account if needed?
7. Hook titles / commentary for originality: auto-generated hook only, or do you want to add your own text/voice?
8. Notifications: push to your phone when clips are ready? Review on phone or PC?
9. Subdomain `clips.bashir.my.id` OK? Private login with your existing account (`bashirsyauqi`) only, or wife too?

## 12. Risks

- YouTube blocking downloads from the VPS IP (most likely failure; needs maintenance).
- Platform rule changes (originality, API audits); campaign rules differ per campaign.
- Free-tier changes (Groq/Gemini limits).
- Income uncertainty: average clipper earnings are low; consistency matters more than tooling.
- Resource contention with live apps → enforced by systemd limits.
