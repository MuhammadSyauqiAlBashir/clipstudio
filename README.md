# Clip Studio

Private clipping tool: long videos (pasted links, campaign files and automatically watched YouTube, Twitch and Kick channels, new uploads and live
streams) → best moments as 9:16 clips with captions → owner review → posting (manual first). Planned at
https://clips.bashir.my.id on the owner's VPS.

**Status: design done, interview done (2026-10-04); Phase 1 next.**

| Doc | What |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Working context for Claude sessions: rules, decisions, planned server facts, open tasks |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Adapted plan: architecture, data model, pipeline, watchers, server budget, free services, phases, interview questions |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Findings with sources: open-source clippers, Groq/Gemini, WebSub/RSS, yt-dlp/ytarchive, detection models, platform APIs, earnings |
| [`docs/SPEC-original.md`](docs/SPEC-original.md) | The owner's original build spec |

Hard rules: permission-based sources only (default blocked), no movie/TV/studio/sports, music excluded, nothing
publishes without approval, credit creators.
