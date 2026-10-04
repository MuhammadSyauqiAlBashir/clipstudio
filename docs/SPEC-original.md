# Clip Automation Pipeline — Build Spec (owner's original, received 2026-10-03)

> Kept word for word as the owner wrote it. The adapted plan that fits this server, the free services and
> the platform rules is in [`DESIGN.md`](DESIGN.md); the research behind the changes is in [`RESEARCH.md`](RESEARCH.md).

**Target runtime:** Your VPS (already running Claude Code).

**Goal:** A mostly-automated system that ingests long-form video (manual links + auto-watched channels/streams), finds the best moments, crops them to vertical with captions, queues them on a review website for your approval, and on approval publishes to YouTube Shorts, TikTok, and Instagram Reels.

**Core principle:** Permission-based sources only. No copyright bypass. No movie/TV footage without a license.

## 1. System Overview

Seven stages: source ingestion (manual links + auto channel/stream watchers), recorder/downloader, transcription (Whisper, word-level timestamps), highlight scorer, clipper (cut + 9:16 crop + captions), review website (your approve/reject), publisher (on approval, uploads to the three platforms).

## 2. Source Ingestion

### 2a. Manual sources (always available)

- Form on the review site to paste a YouTube/Twitch/Kick video or VOD URL for clipping.
- Field to add a channel/streamer to the watch list (by handle/URL).
- Manual URLs jump the queue (processed first).

### 2b. Automatic sources

- Live-stream watcher: poll watch-list channels; when one goes live, start recording.
- New-upload watcher: poll watch-list channels for newly uploaded long-form videos; download and process.
- Polling via YouTube Data API (uploads + live status) and Twitch/Kick APIs.

### 2c. Source eligibility gate (REQUIRED — copyright safety)

- Twitch/Kick: clipping enabled; prefer streamers who explicitly permit off-platform reposting.
- YouTube: creator permits clips / Creative Commons / your own content / campaign-authorized footage.
- Per-channel clip_permission flag: explicit, platform-default, campaign, blocked. Blocked is never recorded.
- No movie/TV/studio footage. No sports leagues/broadcasters. Hard reject.

**Acceptance criteria:** Adding a channel without a clip_permission value defaults to blocked until set. A source failing the gate never reaches the recorder and is logged with the reason.

## 3. Recorder / Downloader

- yt-dlp / streamlink for VOD download and live capture.
- Record to a working directory; store source metadata (channel, URL, permission flag, timestamp).

**Acceptance criteria:** A live stream on the watch list is captured start-to-finish without manual action. A pasted VOD URL downloads and enters the pipeline within one polling cycle.

## 4. Transcription

- Whisper (self-hosted, free), word-level timestamps.
- Output feeds the highlight scorer and the caption burner.

**Acceptance criteria:** Every recorded source produces a word-level transcript before scoring.

## 5. Highlight Scorer (the "is this a good shot?" brain)

Score each candidate moment on stacked signals; only clip where several line up above a threshold.

**Audio signals:** loudness spikes (sudden volume jumps above rolling baseline — laughs, shouts, hype, crowd reactions); laughter detection as its own audio pattern; pace changes.

**Content signals (transcript via LLM):** punchlines, surprising statements, strong opinions, question-and-payoff, emotional beats.

**Optional (later):** facecam expression spikes.

**Music-avoidance filter (REQUIRED):** detect music; down-rank or exclude segments with recognizable music, since platforms auto-mute/claim/block them even with streamer permission.

**Scoring:** composite score across signals; only moments above threshold AND clear of music become clips. LLM ranking runs on a local model (free) or paid API (Claude/GPT) — configurable.

**Acceptance criteria:** A loud laugh aligned with a transcript punchline scores higher than loud-only or text-only. A segment containing music is excluded even if otherwise high-scoring.

## 6. Clipper

- Build on self-hostable open-source (free, no watermark): OpenShorts (MIT) and/or SupoClip, plus the Whisper + LLM + auto-crop GitHub stack.
- 9:16 vertical crop with face tracking (MediaPipe + OpenCV) so the speaker stays framed.
- Burned captions from word-level timestamps.
- Cut on natural thought boundaries, not mid-sentence.

**Acceptance criteria:** Output is 9:16, speaker stays framed, captions synced, length about 15 to 60 seconds.

## 7. Review Website (your approval step)

- Web UI listing queued clips: preview player, source, score, caption text, detected signals.
- Approve / Reject / Edit caption per clip.
- Approve triggers the publisher. Reject archives with optional reason.
- Manual-source submission form (section 2a) lives here too.

**Acceptance criteria:** You can watch, approve, and reject from the browser. Approval and only approval starts an upload. Nothing publishes unattended.

## 8. Publisher

- On approval, upload to YouTube Shorts, TikTok, Instagram Reels via official APIs.
- Per-platform caption/hashtag templates; credit the original creator in the caption.
- Caution: TikTok and YouTube restrict automated posting — use official Content Posting APIs, respect rate limits, keep your approval in the loop, stagger uploads.

**Acceptance criteria:** One approval publishes to all three platforms (or the enabled subset) in correct vertical format with creator credit. Upload failures are logged and retried without duplicating posts.

## 9. Monetization Setup Guide (manual — do these yourself)

**YouTube Shorts:** join the YouTube Partner Program — 1,000 subscribers + 10M valid public Shorts views in 90 days (Shorts path) or 4,000 watch hours (long-form path); earnings via Shorts ad revenue sharing; set up Google AdSense.

**TikTok:** Creativity Program / Creator Rewards — typically 10,000 followers + 100,000 views in 30 days, age 18+. Faster cash: clipping campaigns (Whop, Vyro, Reach.cat) pay per verified view with no follower minimum.

**Instagram Reels:** bonuses/ads where available by region, plus affiliate and brand deals; check the Instagram Professional Dashboard (varies by country — confirm for Indonesia).

**Fastest-cash path:** join clipping campaigns that provide authorized footage and pay per view while your own channels grow toward native monetization.

## 10. Tech Stack Summary

- Download/record: yt-dlp, streamlink
- Transcribe: Whisper (self-hosted)
- Clip/crop/caption: OpenShorts, SupoClip, MediaPipe, OpenCV, FFmpeg
- Highlight LLM: local model (free) or Claude/GPT API (optional)
- Review site: your choice of web framework on the VPS
- Publish: YouTube Data API, TikTok Content Posting API, Instagram Graph API
- Hosting: your existing VPS

## 11. Build Order

1. Manual-link ingestion, recorder, transcription, clipper, review site (approve only, no publish). Prove clip quality first.
2. Add publisher (one platform, then all three).
3. Add highlight scorer with music filter.
4. Add automatic channel/stream watchers last.

## 12. Hard Rules

- Permission-based sources only; clip_permission gate enforced.
- No movie/TV/studio/sports footage.
- Music segments excluded.
- Nothing publishes without your approval.
- Credit original creators.
