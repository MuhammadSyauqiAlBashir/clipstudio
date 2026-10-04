# Clip Studio — research (2026-10-03 → 10-04)

Everything the plan in [`DESIGN.md`](DESIGN.md) is based on, with sources. Re-check the fast-moving items (platform
rules, free-tier limits, yt-dlp behaviour) before relying on them; they change often.

## 1. Existing open-source clippers (none can run as-is here)

| Project | License | Stack | Requirements | What we reuse (ideas, not code) |
|---|---|---|---|---|
| [OpenShorts (mutonby)](https://github.com/mutonby/openshorts) | MIT core (`cloud/` source-available) | Python 3.11 + FastAPI, React, faster-whisper, YOLOv8 + MediaPipe, Gemini Flash-Lite for moments, FFmpeg, Docker Compose, S3 | **"8GB+ RAM"**; CPU "5 to 8 min per 8-min video" | Gemini on the transcript + scene boundaries → 3–15 moments; layout modes TRACK / GENERAL (blurred bg) / SPLIT (2 speakers) / SCREENCAST |
| [SupoClip (FujiwaraChoki)](https://github.com/FujiwaraChoki/supoclip) | **AGPL-3.0** (copying code would make our app AGPL) | FastAPI, Next.js, Docker, **PostgreSQL + Redis**, ARQ worker, AssemblyAI transcription | Docker stack, too heavy | Scoring + hook titles, word-synced subtitle templates |
| [AutoClip (artbyjazi)](https://github.com/artbyjazi/autoclip) | MIT (+ OFL fonts) | Python 3.11–3.12 (**not 3.13: no MediaPipe wheels**), FastAPI, Whisper, MediaPipe, FFmpeg + libass, yt-dlp, optional Ollama/WhisperX | GPU optional; local LLM ~2.3 GB | **Highlights returned as word indices, not timestamps** (more reliable); shot detection before crop interpolation; per-job cached, resumable artifacts; 4 caption styles |
| [AI-Youtube-Shorts-Generator (Anil-matcha)](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator) | see repo | Whisper + GPT highlight detection + OpenCV face tracking with motion smoothing | — | Smoothing of the crop path |
| [opensource-clipping (TaimourKhan2132)](https://github.com/TaimourKhan2132/opensource-clipping) | see repo | Whisper, Gemini, MediaPipe, Pyannote, karaoke subtitles, B-roll, auto-uploaders | — | Kinetic karaoke subtitle ideas |
| [ffmpeg-caption-burn-ass (nik-devs)](https://github.com/nik-devs/ffmpeg-caption-burn-ass) | see repo | **Pure Python stdlib + FFmpeg/libass**, input `{"word","start","end"}` | none | Exactly our caption approach: one `.ass` file, karaoke-highlight (3–5 words, active word tinted/scaled) or word-carousel, burned in one FFmpeg pass |

**Conclusion:** build a lean pipeline in our own style (FastAPI + plain JS like the other apps), borrowing ideas.
Don't install OpenShorts/SupoClip; their Docker stacks + local models need 8 GB+ RAM. Avoid copying AGPL code.

## 2. Transcription

| Option | Fit | Notes |
|---|---|---|
| **Groq Whisper `whisper-large-v3-turbo` (free tier)** | ✅ primary | Free: **20 req/min, 2,000 req/day, 7,200 audio s/hour, 28,800 audio s/day (8 h/day)**. Word timestamps: `response_format=verbose_json` + `timestamp_granularities=["word"]` (added 2025-03-11). Upload size is limited per request (send compressed mono audio, ~32 kbps Opus/MP3; split long audio into chunks with overlap) ([Groq STT docs](https://console.groq.com/docs/speech-to-text), [changelog](https://console.groq.com/docs/legacy-changelog), [limits overview](https://www.eesel.ai/blog/groq-pricing)) |
| faster-whisper on this CPU | ⚠️ fallback only | `small` int8 needs ~1 GB RAM; benchmark 13 min audio in 102 s on **8 threads**; on our 2 vCPU expect roughly real time or slower ([faster-whisper](https://github.com/AIXerum/faster-whisper), [benchmarks](https://localaimaster.com/blog/faster-whisper-guide)). `tiny`/`base` ~15–20× real time on CPU but weaker Indonesian |
| WhisperX | ❌ | Better word alignment (wav2vec2) but too heavy for 2 GB |
| Cloudflare Workers AI Whisper | ❌ | Would eat the shop's free FLUX neurons (same account) |

## 3. Watching YouTube channels (uploads + live)

| Method | Cost | Notes |
|---|---|---|
| **WebSub / PubSubHubbub push** | free, no quota | POST to `https://pubsubhubbub.appspot.com/subscribe` with topic `https://www.youtube.com/xml/feeds/videos.xml?channel_id=UC…` + our public HTTPS callback; hub verifies with `hub.challenge` (reply 200 + the challenge body); then POSTs an Atom entry when a video is uploaded **or its title/description changes** (dedupe!). Subscriptions expire (lease) → renew. Live streams/premieres also appear as entries ([YouTube push guide](https://developers.google.com/youtube/v3/guides/push_notifications), [webhook guide](https://vidproxy.pro/youtube-webhook)) |
| **Channel RSS feed** `https://www.youtube.com/feeds/videos.xml?channel_id=UC…` | free, no quota, no key | 15 latest items; undocumented; had intermittent 404s from Dec 2025, working again by May 2026 → use as a safety-net poll (every 10–15 min) ([RSS guide](https://www.wprssaggregator.com/youtube-rss-feed/), [limits](https://rsscribe.com/blog/youtube-rss-feeds-explained)) |
| YouTube Data API v3 | 10,000 units/day free | `videos.list` = **1 unit, up to 50 IDs** → check `liveStreamingDetails` / `liveBroadcastContent` / `duration` of IDs from WebSub/RSS. **Never `search.list` (100 units)**. `playlistItems.list` (uploads playlist) = 1 unit ([quota guide](https://dev.to/siyabuilt/youtubes-api-quota-is-10000-unitsday-heres-how-i-track-100k-videos-without-hitting-it-5d8h), [costs](https://outlierkit.com/resources/youtube-search-api/)) |
| Scraping `/@handle/live` or yt-dlp `--wait-for-video` | free | Works without API ([StreamAlerter](https://github.com/dec4234/StreamAlerter)) but fragile and counts as bot traffic from a datacenter IP |

## 4. Recording / downloading

- **yt-dlp**: VOD download; `--live-from-start` (experimental) records a live stream from the start of the DVR window;
  `--wait-for-video 30` waits for a scheduled stream. A growing live download never stops on its own and yt-dlp
  doesn't always notice the end ([yt-dlp](https://github.com/yt-dlp/yt-dlp/), [live guide](https://tubefetcher.com/blog/download-youtube-live-streams/)).
- **ytarchive**: purpose-built live archiver (`--wait`, `--monitor-channel`, `--retry-stream`, `--cookies`). The original
  binary broke on YouTube's nsig challenge; a maintained replacement uses yt-dlp + deno + ffmpeg
  ([Kethsar/ytarchive](https://github.com/Kethsar/ytarchive), [Fab-H2O/yt-archive](https://github.com/Fab-H2O/yt-archive));
  [hoshinova](https://github.com/HoloArchivists/hoshinova) monitors channels and runs ytarchive.
- **streamlink**: records the live edge reliably; restart in a loop to avoid gaps
  ([recording gist](https://gist.github.com/glubsy/744d3f91b80347b3f684d3dc2fcb12e2)).
- **Datacenter IP blocking (big risk):** YouTube treats VPS IPs as bots ("Sign in to confirm you're not a bot").
  Mitigations: latest yt-dlp + a JS runtime (deno) for nsig, a PO-token provider
  ([bgutil-ytdlp-pot-provider](https://pypi.org/project/bgutil-ytdlp-pot-provider)), cookies from a **throwaway**
  YouTube account (risk: that account can be banned), slow request rates. Expect breakage and maintenance
  ([ytdlp.org server guide](https://ytdlp.org/guides/run-yt-dlp-server), [bot error guide](https://ytdlp.org/guides/fix-sign-in-to-confirm-not-a-bot)).

## 5. Highlight signals

- **Loudness**: FFmpeg `ebur128`/`astats` → short-term loudness per 0.5 s vs a rolling baseline (spikes = laughs,
  shouts, hype). Nearly free on CPU.
- **Laughter**: [jrgillick/laughter-detection](https://github.com/jrgillick/laughter-detection) (PyTorch, noise-robust
  model) — accurate but PyTorch is heavy for 2 GB; alternative: ask Gemini about laughter on short candidate audio.
- **Music**: [inaSpeechSegmenter](https://github.com/ina-foss/inaSpeechSegmenter) (MIT, INA) splits audio into
  speech / music / noise, **~30× real time on a laptop CPU** (1 h in ~2 min) but needs TensorFlow (large install,
  ~0.5–1 GB RAM while running). Alternatives: Gemini audio check on each candidate segment (cheap, no local model),
  or YAMNet (TFLite, light).
- **Transcript (LLM)**: Gemini picks punchlines, surprises, strong opinions, question→payoff, emotional beats; return
  **word index ranges** (AutoClip's trick) so cuts land on words; snap to sentence/pause boundaries.

## 6. Framing and captions

- MediaPipe face detection on sampled frames (e.g. 4–5 fps) → smoothed crop path → FFmpeg crop/scale to 1080×1920 (or
  720×1280 to save CPU). Fallbacks: no face → blurred-background "GENERAL" layout; 2 faces → SPLIT. MediaPipe needs
  Python ≤ 3.12 wheels.
- Captions: generate `.ass` (libass) karaoke-highlight from word timings, burn with one FFmpeg pass (`subtitles=`
  filter). Bundle an open font (e.g. Anton/Inter under OFL).

## 7. Publishing APIs (why posting stays manual at first)

| Platform | Reality |
|---|---|
| YouTube | Uploads via `videos.insert` from **unverified API projects are locked to private** until the project passes a compliance audit; locked videos can't be appealed, only re-uploaded. Upload costs 1,600 quota units (~6/day on the free 10,000) ([API docs](https://developers.google.com/youtube/v3/docs/videos), [private lock](https://clipember.com/guides/youtube-upload-comes-out-private)) |
| TikTok | Content Posting API unaudited: **SELF_ONLY (private) posts only**, max 5 users/24 h, account must be private; **"Upload to inbox" (drafts) works without audit** ([guide](https://www.outstand.so/blog/tiktok-content-posting-api), [audit](https://bundle.social/blog/tiktok-api-approval)) |
| Instagram | Graph API publishing needs a Professional (Business/Creator) account linked to a Facebook Page + a Meta app |

## 8. Money (what's realistic)

- **Clipping campaigns** (Whop Content Rewards, Vyro, etc.): listings $0.20–$6 per 1,000 views, typically
  $0.50–$1.50; independent tracking: $2.58M paid to 8,466 earners over 6.6B views → **~$0.39 per 1,000 views and
  ~$305 lifetime per clipper on average** (per-clip caps, competition). Sources are mostly clipping-tool vendors,
  so "beginners $100–500/month" is optimistic ([OpenClip](https://openclip.app/guides/how-much-do-clippers-make),
  [Whop guide](https://openclip.app/guides/whop-clipping-guide), [OpusClip](https://www.opus.pro/blog/whop-content-rewards)).
- **YouTube originality push (1 Oct 2026):** Shorts recommendations now favour original material and **cut
  distribution for channels that mainly repost other creators' videos without significant changes**; the reused-
  content policy allows clips/compilations only with added original value (commentary, edits)
  ([ppc.land](https://ppc.land/re-uploaded-shorts-lose-reach-as-youtube-favours-original-clips/),
  [AndroidHeadlines](https://www.androidheadlines.com/2026/10/youtube-cuts-reach-reuploaded-shorts-originality-push.html),
  [vidIQ](https://vidiq.com/blog/post/youtube-reused-content-policy-guide/)).
- **TikTok Creator Rewards**: 18+, 10k followers, 100k views/30 days; **availability in Indonesia unclear (sources
  conflict)** → check TikTok Studio → Monetization ([Toptal](https://www.toptal.com/creator/post/how-to-join-the-tiktok-creator-rewards-program),
  [Multilogin](https://multilogin.com/blog/mobile/tiktok-creator-rewards-program/)).

## 9. Server measurements (2026-10-03)

2 vCPU, 1.97 GB RAM (+2 GB swap), 47 GB free disk. Apps together ~350 MB; Claude Code sessions ~585 MB and the VS Code
remote server ~300 MB are the big users. With Claude/VS Code open, idle apps get swapped (first request 2.4 s, then
0.11 s). CPU ~90% idle. See DESIGN.md §6 for the budget.
