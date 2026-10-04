// Clip Studio PWA: review clips, get finals, add sources and channels.
import { $, api, ago, busy, el, gb, mb, mmss, setAuthHandler, sheet, sleep, toast } from "./lib.js"

const app = $("#app")
const tabs = $("#tabs")
let me = null
let refreshTimer = null

const PERMS = [
  ["explicit", "Explicit — the creator said clipping is OK"],
  ["campaign", "Campaign — a clipping campaign gave the footage"],
  ["platform-default", "Platform default — Creative Commons / own content"],
  ["implied", "Implied — the creator welcomes clips, no written permission"],
]
const STATUS = {
  queued: ["Queued", ""], uploading: ["Uploading", ""], downloading: ["Downloading", "warn"], recording: ["🔴 Recording", "bad"],
  transcribing: ["Transcribing", "warn"], scoring: ["Finding moments", "warn"], rendering: ["Making previews", "warn"],
  review: ["Clips ready", "good"], done: ["Done", ""], failed: ["Failed", "bad"], rejected_by_gate: ["Refused by the gate", "bad"],
  waiting_replay: ["Waiting for replay", ""],
}
const PLATFORM_ICON = { youtube: "▶️", twitch: "🟣", kick: "🟢", upload: "📁", other: "🔗" }

setAuthHandler(() => { me = null; render() })

// ---------------------------------------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------------------------------------
const TABS = [["review", "🎬", "Review"], ["ready", "✅", "Ready"], ["sources", "➕", "Add"], ["campaigns", "🎯", "Campaigns"], ["stats", "📈", "Stats"], ["more", "⚙️", "More"]]

function drawTabs(active, counts = {}) {
  tabs.hidden = false
  tabs.replaceChildren(...TABS.map(([id, ic, label]) => el("a", { href: `#${id}`, class: id === active ? "on" : "" },
    el("b", { text: ic }), label, counts[id] ? el("i", { text: counts[id] }) : null)))
}

async function render() {
  clearTimeout(refreshTimer)
  if (!me) {
    try { me = (await api("/me", { quiet: true })).me } catch (_) { me = null }
  }
  if (!me) return loginPage()
  const [path, query] = (location.hash.slice(1) || "review").split("?")
  const [page, arg] = path.split("/")
  const back = new URLSearchParams(query || "").get("tiktok")
  if (back) {
    history.replaceState(null, "", "#more")
    setTimeout(() => toast(back === "connected" ? "TikTok connected ✅" : `TikTok: ${back}`, back === "connected" ? "" : "bad"), 300)
  }
  const active = { source: "sources", clip: "ready", channels: "more", browse: "sources" }[page] || page
  let counts = {}
  try {
    const c = (await api("/clips?status=review")).counts || {}
    counts = { review: c.review || 0, ready: (c.ready || 0) + (c.approved || 0) + (c.rendering || 0) }
  } catch (_) {}
  drawTabs(active, counts)
  const view = el("div")
  try {
    if (page === "review") await reviewPage(view)
    else if (page === "ready") await readyPage(view)
    else if (page === "sources") await sourcesPage(view)
    else if (page === "source") await sourcePage(view, +arg)
    else if (page === "channels") await channelsPage(view)
    else if (page === "browse") await browsePage(view)
    else if (page === "stats") await statsPage(view)
    else if (page === "campaigns") await campaignsPage(view)
    else if (page === "more") await morePage(view)
    else if (page === "clip") { await readyPage(view); openClip(+arg) }
    else { location.hash = "review"; return }
  } catch (e) {
    view.replaceChildren(el("div", { class: "empty" }, el("b", { text: "⚠️" }), e.message))
  }
  app.replaceChildren(view)
}

function autoRefresh(ms) {
  clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    const playing = [...document.querySelectorAll("video")].some((v) => !v.paused)
    if (document.querySelector("dialog[open]") || playing) autoRefresh(ms)
    else render()
  }, ms)
}

window.addEventListener("hashchange", render)
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && me) render() })
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {})
  navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.type === "navigate") location.hash = e.data.url.split("#")[1] || "review" })
}

// ---------------------------------------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------------------------------------
function loginPage() {
  tabs.hidden = true
  const u = el("input", { autocomplete: "username", autocapitalize: "none", placeholder: "Username" })
  const p = el("input", { type: "password", autocomplete: "current-password", placeholder: "Password" })
  const go = el("button", { class: "btn primary", type: "submit", text: "Log in" })
  const form = el("form", { class: "card col login" }, el("div", { class: "logo", text: "✂️" }), el("h1", { text: "Clip Studio", style: { textAlign: "center" } }),
    el("p", { class: "muted small", style: { textAlign: "center", margin: 0 }, text: "Use your household account." }), u, p, go,
    el("p", { class: "small", style: { textAlign: "center", margin: 0 } }, el("a", { href: "/privacy.html", text: "Privacy" }), " · ", el("a", { href: "/terms.html", text: "Terms" })))
  form.onsubmit = (e) => {
    e.preventDefault()
    busy(go, async () => { me = (await api("/login", { method: "POST", json: { username: u.value.trim(), password: p.value } })).me; location.hash = "review"; render() }).catch(() => {})
  }
  app.replaceChildren(form)
}

// ---------------------------------------------------------------------------------------------------------
// Clip cards
// ---------------------------------------------------------------------------------------------------------
function signals(c) {
  return el("div", { class: "row wrap", style: { gap: "5px", marginTop: "6px" } },
    el("span", { class: "chip", text: `⏱ ${Math.round(c.duration)}s` }),
    el("span", { class: "chip", text: `🧠 ${c.llm_score}/10` }),
    c.loud >= 1.5 ? el("span", { class: "chip warn", text: "🔊 loud" }) : null,
    c.laughter ? el("span", { class: "chip good", text: "😂 laughter" }) : null,
    c.reaction ? el("span", { class: "chip good", text: "😮 reaction" }) : null,
    c.music ? el("span", { class: c.music === "none" ? "chip" : "chip bad", text: c.music === "none" ? "🎵 no music" : `🎵 ${c.music}` }) : null,
    c.layout ? el("span", { class: "chip", text: { track: "🎯 tracking", split: "👥 split", general: "🖼 full frame", vertical: "📱 vertical" }[c.layout] || c.layout }) : null)
}

function poster(c) {
  const b = el("button", { class: "poster", type: "button", "aria-label": "Play" },
    el("img", { src: `/api/clips/${c.id}/thumb.jpg`, alt: "", loading: "lazy" }), el("span", { text: "▶" }))
  b.onclick = () => {
    const v = el("video", { src: `/api/clips/${c.id}/preview.mp4`, controls: true, playsinline: true, autoplay: true })
    b.replaceWith(v)
  }
  return c.has_preview ? b : el("div", { class: "poster", style: { display: "grid", placeItems: "center", color: "#fff" }, text: "—" })
}

function reviewCard(c, onDone) {
  const card = el("div", { class: "card clip" })
  const approve = el("button", { class: "btn", type: "button", text: "✅ Approve" })
  const reject = el("button", { class: "btn bad", type: "button", text: "✕ Reject" })
  const edit = el("button", { class: "btn", type: "button", text: "✏️ Edit" })
  approve.onclick = () => busy(approve, async () => { await api(`/clips/${c.id}/approve`, { method: "POST" }); toast("Approved — making the full-quality clip"); onDone() }).catch(() => {})
  reject.onclick = () => rejectSheet(c, onDone)
  edit.onclick = () => editSheet(c, onDone)
  card.append(
    el("div", { class: "media" }, poster(c),
      el("div", { class: "col", style: { gap: "4px", minWidth: 0 } },
        el("div", { class: "row" }, el("span", { class: "score", text: Math.round(c.score) }), el("span", { class: "muted small grow", text: "score" })),
        el("div", { class: "hook", text: c.hook || "(no hook)" }),
        c.owner_text ? el("div", { class: "small", text: `💬 ${c.owner_text}` }) : null,
        el("div", { class: "small muted", text: c.reason }),
        signals(c),
        el("div", { class: "small muted ellipsis", style: { marginTop: "6px" }, text: `${PLATFORM_ICON[c.source.platform] || ""} ${c.source.creator || ""} · ${c.source.title}` }))),
    el("div", { class: "actions" }, reject, edit, approve))
  return card
}

function rejectSheet(c, onDone) {
  const { body, close } = sheet("Reject clip")
  const reasons = ["Boring", "Cut is off", "Wrong framing", "Music / copyright", "Needs context", "Duplicate"]
  const other = el("input", { placeholder: "Other reason (optional)" })
  const send = async (reason, btn) => busy(btn, async () => { await api(`/clips/${c.id}/reject`, { method: "POST", json: { reason } }); close(); toast("Rejected"); onDone() }).catch(() => {})
  body.append(el("p", { class: "muted small", text: "Your reasons help tune the scoring later." }),
    el("div", { class: "btns" }, reasons.map((r) => { const b = el("button", { class: "btn", type: "button", text: r }); b.onclick = () => send(r, b); return b })),
    el("div", { class: "row", style: { marginTop: "12px" } }, other, (() => { const b = el("button", { class: "btn bad", type: "button", text: "Reject" }); b.onclick = () => send(other.value, b); return b })()))
}

function editSheet(c, onDone) {
  const { body, close } = sheet("Edit clip")
  const hook = el("input", { value: c.hook, maxlength: 120 })
  const own = el("input", { value: c.owner_text, maxlength: 160, placeholder: "e.g. This is why I quit 9-to-5 😅" })
  const cap = el("textarea", { value: c.caption, maxlength: 600 })
  const tags = el("input", { value: c.hashtags, maxlength: 300 })
  const save = el("button", { class: "btn primary", type: "button", text: "Save" })
  save.onclick = () => busy(save, async () => {
    await api(`/clips/${c.id}`, { method: "PATCH", json: { hook: hook.value, owner_text: own.value, caption: cap.value, hashtags: tags.value } })
    close(); toast(c.status === "ready" ? "Saved — remaking the final with your text" : "Saved"); onDone()
  }).catch(() => {})
  body.append(c.has_preview ? el("video", { class: "big-video", src: `/api/clips/${c.id}/preview.mp4`, controls: true, playsinline: true }) : null,
    el("div", { class: "col" },
      el("label", { class: "field" }, "Hook title (first 3 seconds, on screen)", hook),
      el("label", { class: "field" }, "Your own text on the clip (optional)", own, el("span", { class: "hint", text: "Adds originality (YouTube favours it). Shown at the top after the hook." })),
      el("label", { class: "field" }, "Post caption", cap),
      el("label", { class: "field" }, "Hashtags", tags),
      el("p", { class: "hint", text: "The preview keeps the old text; the full-quality clip uses your edits." }),
      save))
}

// ---------------------------------------------------------------------------------------------------------
// Review
// ---------------------------------------------------------------------------------------------------------
async function reviewPage(view) {
  const { clips } = await api("/clips?status=review")
  view.append(el("div", { class: "topbar" }, el("h1", { text: "Review" }), el("span", { class: "muted small", text: `${clips.length} waiting` })))
  if (!clips.length) {
    view.append(el("div", { class: "empty" }, el("b", { text: "🍿" }), "Nothing to review yet.", el("br"),
      el("a", { href: "#sources", text: "Add a video link" })))
    return
  }
  view.append(el("p", { class: "muted small", style: { marginTop: 0 }, text: "Low-quality previews. Approving makes the 1080×1920 version." }))
  for (const c of clips) view.append(reviewCard(c, render))
}

// ---------------------------------------------------------------------------------------------------------
// Ready (approved → final → post)
// ---------------------------------------------------------------------------------------------------------
async function readyPage(view) {
  const filter = sessionStorage.getItem("readyFilter") || "approved"
  const { clips } = await api(`/clips?status=${filter}`)
  view.append(el("h1", { text: "Ready to post" }),
    el("div", { class: "seg" }, [["approved", "To post"], ["posted", "Posted"], ["rejected", "Rejected"]].map(([k, l]) =>
      el("button", { class: `btn sm ${k === filter ? "on" : ""}`, type: "button", text: l, onclick: () => { sessionStorage.setItem("readyFilter", k); render() } }))))
  if (!clips.length) { view.append(el("div", { class: "empty" }, el("b", { text: "📭" }), "Nothing here yet.")); return }
  let rendering = false
  for (const c of clips) {
    if (c.status === "approved" || c.status === "rendering" || Object.values(c.posts || {}).some((v) => ["queued", "uploading", "processing"].includes(v.status))) rendering = true
    const st = { approved: ["⏳ Waiting for the final render", "warn"], rendering: ["⚙️ Making 1080×1920…", "warn"], ready: ["✅ Ready", "good"],
      posted: ["📤 Posted", "good"], rejected: ["✕ Rejected", "bad"], expired: ["⌛ Expired", ""] }[c.status] || [c.status, ""]
    const card = el("div", { class: "card clip" },
      el("div", { class: "media" }, poster(c), el("div", { class: "col", style: { gap: "5px", minWidth: 0 } },
        el("span", { class: `chip ${st[1]}`, text: st[0], style: { alignSelf: "flex-start" } }),
        el("div", { class: "hook", text: c.hook }),
        c.reject_reason ? el("div", { class: "small muted", text: `Reason: ${c.reject_reason}` }) : null,
        c.note ? el("div", { class: "small", style: { color: "var(--bad)" }, text: c.note }) : null,
        Object.keys(c.posted || {}).length ? el("div", { class: "small muted", text: `Posted on: ${Object.keys(c.posted).join(", ")}` }) : null,
        Object.entries(c.posts || {}).filter(([, v]) => v.status !== "done").map(([k, v]) => el("div", { class: "small", style: { color: v.status === "failed" ? "var(--bad)" : "var(--muted)" },
          text: `${k}: ${v.status === "failed" ? "failed — open to retry" : v.status === "queued" && v.at * 1000 > Date.now() ? `🗓 ${new Date(v.at * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "posting…"}` })),
        el("div", { class: "small muted ellipsis", text: `${c.source.creator} · ${c.source.title}` }))),
      el("div", { style: { padding: "0 12px 12px" } },
        el("button", { class: "btn primary", style: { width: "100%" }, type: "button", text: c.status === "ready" || c.status === "posted" ? "Open · download · copy caption" : "Open", onclick: () => openClip(c.id) })))
    view.append(card)
  }
  if (rendering) autoRefresh(8000)
}

async function shareOrDownload(c, btn) {
  const url = `/api/clips/${c.id}/final.mp4?download=1`
  if (navigator.canShare && window.File) {
    await busy(btn, async () => {
      const blob = await (await fetch(url, { credentials: "same-origin" })).blob()
      const file = new File([blob], `clip-${c.id}.mp4`, { type: "video/mp4" })
      if (navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file] }) } catch (e) { if (e.name !== "AbortError") throw e }
        return
      }
      location.href = url
    }).catch(() => {})
  } else location.href = url
}

async function openClip(id) {
  let c, accounts
  try { c = (await api(`/clips/${id}`)).clip; accounts = (await api("/accounts")).accounts } catch (e) { toast(e.message, "bad"); return }
  const { body, close } = sheet(c.hook || "Clip", { onClose: render })
  const src = c.has_final ? `/api/clips/${c.id}/final.mp4` : c.has_preview ? `/api/clips/${c.id}/preview.mp4` : null
  body.append(src ? el("video", { class: "big-video", src, controls: true, playsinline: true }) : null)
  if (c.has_final) {
    const share = el("button", { class: "btn primary", type: "button", text: "📲 Save / share video" })
    share.onclick = () => shareOrDownload(c, share)
    body.append(el("div", { class: "btns" }, share, el("a", { class: "btn", href: `/api/clips/${c.id}/final.mp4?download=1`, text: `⬇️ Download (${mb(c.final_size)})` })),
      el("p", { class: "hint", text: "On iPhone: Save / share → Save Video puts it in Photos; or share straight to TikTok/Instagram." }))
  } else if (c.status === "approved" || c.status === "rendering") {
    body.append(el("p", { class: "muted", text: "The full-quality version is being made. You'll get a notification." }))
  }
  for (const [p, label] of [["tiktok", "TikTok"], ["youtube", "YouTube Shorts"], ["instagram", "Instagram Reels"]]) {
    const copy = el("button", { class: "btn sm", type: "button", text: "Copy" })
    copy.onclick = async () => { try { await navigator.clipboard.writeText(c.captions[p]); toast(`${label} caption copied`) } catch (_) { toast("Copy failed — long-press the text", "bad") } }
    const mark = el("button", { class: "btn sm", type: "button", text: c.posted[p] ? "✓ Posted" : "Mark posted" })
    mark.onclick = async () => {
      const link = prompt(`Link to the ${label} post (optional):`, c.posted[p] && c.posted[p] !== "posted" ? c.posted[p] : "")
      if (link === null) return
      try { c = (await api(`/clips/${c.id}/posted`, { method: "POST", json: { platform: p, url: link } })).clip; mark.textContent = "✓ Posted"; toast("Saved") } catch (e) { toast(e.message, "bad") }
    }
    const post = (c.posts || {})[p]
    const when = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })
    const scheduled = post && post.status === "queued" && post.at * 1000 > Date.now()
    const st = post ? { queued: scheduled ? `🗓 Scheduled for ${when(post.at)}` : "⏳ Waiting to post", uploading: "⬆️ Uploading…", processing: "⚙️ Processing on the platform…",
      done: p === "tiktok" ? "📥 Sent to your TikTok inbox — open TikTok to post it, then tap Mark posted" : "✅ Posted automatically",
      failed: `⚠️ Failed: ${post.error}` }[post.status] : ""
    const pub = el("button", { class: "btn sm primary", type: "button", text: post && post.status === "failed" ? "Retry" : "Post now" })
    const views = post && post.status === "done" ? (post.stats.views ?? post.stats.reach) : undefined
    pub.onclick = () => busy(pub, async () => { c = (await api(`/clips/${c.id}/publish`, { method: "POST", json: { platform: p } })).clip; pub.remove(); toast("Posting…") }).catch(() => {})
    const canPost = c.has_final && (!post || post.status === "failed" || scheduled) && !(c.posted || {})[p] && accounts && accounts[p] && accounts[p].connected
    body.append(el("div", { class: "card", style: { marginTop: "10px" } },
      el("div", { class: "row" }, el("b", { class: "grow", text: label }), canPost ? pub : null, copy, c.has_final && !(post && post.status === "done" && p !== "tiktok") ? mark : null),
      st ? el("div", { class: "small", style: { marginTop: "4px", color: post.status === "failed" ? "var(--bad)" : "inherit" } }, st,
        post.url ? el("span", {}, " · ", el("a", { href: post.url, target: "_blank", rel: "noopener", text: "open" })) : null,
        views !== undefined ? ` · 👁 ${views.toLocaleString()} views` : "") : null,
      el("pre", { class: "caption", text: c.captions[p] })))
  }
  const edit = el("button", { class: "btn", type: "button", text: "✏️ Edit text" })
  edit.onclick = () => { close(); editSheet(c, render) }
  body.append(el("div", { class: "btns" }, edit,
    c.status === "rejected" ? el("button", { class: "btn good", type: "button", text: "Approve after all", onclick: async (e) => { await busy(e.target, () => api(`/clips/${c.id}/approve`, { method: "POST" })).catch(() => {}); close() } }) : null),
    el("p", { class: "hint", text: `Source: ${c.source.title} — ${c.source.creator} (${c.source.permission})` }))
}

// ---------------------------------------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------------------------------------
function permSelect(value = "") {
  return el("select", {}, el("option", { value: "", text: "Choose your permission…" }), PERMS.map(([v, l]) => el("option", { value: v, text: l, selected: v === value })))
}

function sourceRow(s) {
  const [label, kind] = STATUS[s.status] || [s.status, ""]
  const clips = s.clips || {}
  const n = (clips.review || 0)
  return el("a", { class: "card", href: `#source/${s.id}`, style: { display: "block", color: "inherit", textDecoration: "none" } },
    el("div", { class: "row" }, el("span", { text: PLATFORM_ICON[s.platform] || "🔗" }), s.campaign_id ? el("span", { text: "🎯" }) : null, el("b", { class: "grow ellipsis", text: s.title || s.url || "Upload" }),
      el("span", { class: `chip ${kind}`, text: label })),
    el("div", { class: "small muted ellipsis", style: { marginTop: "4px" }, text: [s.creator, s.duration ? mmss(s.duration) : "", ago(s.created_at), s.kind].filter(Boolean).join(" · ") }),
    s.step ? el("div", { class: "small", style: { marginTop: "4px" }, text: s.step }) : null,
    ["downloading", "transcribing", "scoring", "rendering"].includes(s.status) ? el("div", { class: "progress" }, el("i", { style: { width: `${Math.round(s.progress * 100)}%` } })) : null,
    s.reason ? el("div", { class: "small", style: { marginTop: "4px", color: s.status === "failed" || s.status === "rejected_by_gate" ? "var(--bad)" : "var(--muted)" }, text: s.reason }) : null,
    n ? el("div", { class: "small", style: { marginTop: "4px", color: "var(--good)", fontWeight: 700 }, text: `${n} clip${n > 1 ? "s" : ""} to review` }) : null)
}

async function sourcesPage(view) {
  const url = el("input", { type: "url", placeholder: "https://youtube.com/watch?v=… / twitch.tv/videos/… / kick.com/…", inputmode: "url" })
  const perm = permSelect()
  const proof = el("input", { placeholder: "Proof: link or note (e.g. campaign page, creator's post)" })
  const { campaigns: camps } = await api("/campaigns?show=all")
  const pre = +(sessionStorage.getItem("campaignForUpload") || 0)
  sessionStorage.removeItem("campaignForUpload")
  const camp = el("select", {}, el("option", { value: "", text: "No campaign" }),
    camps.filter((c) => c.joined || c.status === "open").map((c) => el("option", { value: c.id, text: `🎯 ${c.title.slice(0, 60)}`, selected: c.id === pre })))
  camp.onchange = () => { if (camp.value) { perm.value = "campaign"; const c = camps.find((x) => x.id === +camp.value); if (c && !proof.value) proof.value = c.url } }
  if (pre) camp.onchange()
  const add = el("button", { class: "btn primary", type: "button", text: "Make clips" })
  add.onclick = () => busy(add, async () => {
    if (!perm.value) throw new Error("Choose your permission first.")
    await api("/sources", { method: "POST", json: { url: url.value.trim(), permission: perm.value, proof: proof.value, campaign_id: camp.value ? +camp.value : null } })
    url.value = ""; proof.value = ""; toast("Added — it will start in a moment"); render()
  }).catch(() => {})
  const file = el("input", { type: "file", accept: "video/*" })
  const up = el("button", { class: "btn", type: "button", text: "Upload campaign file" })
  const bar = el("i")
  const upProg = el("div", { class: "progress", hidden: true }, bar)
  up.onclick = () => {
    if (!file.files[0]) { toast("Choose a video file first.", "bad"); return }
    if (!perm.value) { toast("Choose your permission first.", "bad"); return }
    const f = file.files[0]
    const q = new URLSearchParams({ name: f.name, permission: perm.value, proof: proof.value, title: f.name.replace(/\.[^.]+$/, ""), campaign_id: camp.value || 0 })
    const x = new XMLHttpRequest()
    x.open("PUT", `/api/uploads?${q}`)
    x.setRequestHeader("X-CS", "1")
    x.upload.onprogress = (e) => { upProg.hidden = false; bar.style.width = `${Math.round((e.loaded / e.total) * 100)}%` }
    x.onload = () => { up.disabled = false; if (x.status === 200) { toast("Uploaded"); render() } else { let m = "Upload failed"; try { m = JSON.parse(x.responseText).error } catch (_) {} toast(m, "bad") } }
    x.onerror = () => { up.disabled = false; toast("Upload failed", "bad") }
    up.disabled = true
    x.send(f)
  }
  view.append(el("div", { class: "topbar" }, el("h1", { text: "Add" }), el("a", { class: "btn sm", href: "#browse", text: "🔎 Browse a YouTube channel" })),
    el("div", { class: "card col" }, el("b", { text: "Add a video" }), url, perm, proof, el("label", { class: "field" }, "Campaign (adds its hashtags to the captions)", camp), add,
      el("details", {}, el("summary", { class: "small", style: { cursor: "pointer", fontWeight: 650 }, text: "…or upload a file (campaign footage)" }),
        el("div", { class: "col", style: { marginTop: "10px" } }, file, up, upProg)),
      el("p", { class: "hint", style: { margin: 0 }, text: "Only videos you're allowed to clip. Movies, TV, studio and sports content is refused automatically." })))
  const { sources } = await api("/sources")
  if (!sources.length) view.append(el("div", { class: "empty" }, el("b", { text: "🎞" }), "No sources yet."))
  for (const s of sources) view.append(sourceRow(s))
  if (sources.some((s) => ["queued", "downloading", "transcribing", "scoring", "rendering", "recording"].includes(s.status))) autoRefresh(6000)
}

async function sourcePage(view, id) {
  const { source: s, clips } = await api(`/sources/${id}`)
  view.append(el("a", { href: "#sources", class: "small", text: "← Sources" }), sourceRow(s))
  const acts = el("div", { class: "btns" })
  if (s.status === "failed") acts.append(el("button", { class: "btn", type: "button", text: "↻ Retry", onclick: (e) => busy(e.target, async () => { await api(`/sources/${id}/retry`, { method: "POST" }); render() }).catch(() => {}) }))
  if (s.url) acts.append(el("a", { class: "btn", href: s.url, target: "_blank", rel: "noopener", text: "Open original" }))
  acts.append(el("button", { class: "btn bad", type: "button", text: "Delete", onclick: async (e) => {
    if (!confirm("Delete this source and all its clips?")) return
    await busy(e.target, () => api(`/sources/${id}`, { method: "DELETE" })).catch(() => {}); location.hash = "sources"
  } }))
  view.append(acts, el("div", { class: "card small" }, el("dl", { class: "kv" },
    el("dt", { text: "Permission" }), el("dd", { text: s.permission }), el("dt", { text: "Proof" }), el("dd", { text: s.proof || "—" }),
    el("dt", { text: "Language" }), el("dd", { text: s.language || "—" }), el("dt", { text: "Files" }), el("dd", { text: s.files_deleted ? "cleaned up" : s.size ? gb(s.size) : "—" }))))
  const live = clips.filter((c) => c.status !== "excluded")
  const out = clips.filter((c) => c.status === "excluded")
  if (live.length) view.append(el("h2", { text: "Clips" }))
  for (const c of live) {
    if (c.status === "review") view.append(reviewCard(c, render))
    else view.append(el("div", { class: "card row", onclick: () => openClip(c.id), style: { cursor: "pointer" } }, el("b", { class: "grow", text: c.hook }), el("span", { class: "chip", text: c.status })))
  }
  if (out.length) {
    view.append(el("h2", { text: `Not used (${out.length})` }))
    view.append(el("div", { class: "card small" }, out.map((c) => el("div", { style: { padding: "6px 0", borderBottom: "1px solid var(--line)" } },
      el("b", { text: `${mmss(c.start)}–${mmss(c.end)} · ${c.hook}` }), el("div", { class: "muted", text: c.note })))))
  }
  if (["queued", "downloading", "transcribing", "scoring", "rendering", "recording"].includes(s.status)) autoRefresh(6000)
}

// ---------------------------------------------------------------------------------------------------------
// Browse: find a YouTube channel and pick videos from its uploads
// ---------------------------------------------------------------------------------------------------------
const nf = new Intl.NumberFormat("en", { notation: "compact" })
const bstate = { q: sessionStorage.getItem("browseQ") || "", channel: sessionStorage.getItem("browseCh") || "", page: "", stack: [], hideShorts: sessionStorage.getItem("hideShorts") !== "0" }

function clipThisSheet(v, ch, watched, onDone) {
  const { body, close } = sheet("Make clips from this video")
  const last = (JSON.parse(localStorage.getItem("permByChannel") || "{}"))[ch.id] || {}
  const campaignId = +(sessionStorage.getItem("browseCampaign") || 0)
  const perm = permSelect(campaignId ? "campaign" : (watched && watched.permission !== "blocked" && watched.permission) || last.permission || "")
  const proof = el("input", { value: campaignId ? `Campaign #${campaignId} in Clip Studio` : last.proof || "", placeholder: "Proof: link or note" })
  const go = el("button", { class: "btn primary", type: "button", text: "Make clips" })
  go.onclick = () => busy(go, async () => {
    if (!perm.value) throw new Error("Choose your permission first.")
    await api("/sources", { method: "POST", json: { url: v.url, permission: perm.value, proof: proof.value, creator: ch.title, campaign_id: campaignId || null } })
    try { const m = JSON.parse(localStorage.getItem("permByChannel") || "{}"); m[ch.id] = { permission: perm.value, proof: proof.value }; localStorage.setItem("permByChannel", JSON.stringify(m)) } catch (_) {}
    close(); toast("Added — clips in about 10 minutes"); onDone()
  }).catch(() => {})
  body.append(el("img", { src: v.thumb, alt: "", style: { width: "100%", borderRadius: "12px", aspectRatio: "16/9", objectFit: "cover" } }),
    el("b", { text: v.title }), el("div", { class: "small muted", style: { margin: "4px 0 12px" }, text: `${ch.title} · ${mmss(v.duration)} · ${nf.format(v.views)} views` }),
    el("div", { class: "col" }, perm, proof, el("p", { class: "hint", style: { margin: 0 }, text: "Your choice is remembered for this channel." }), go))
}

async function browsePage(view) {
  const q = el("input", { value: bstate.q, placeholder: "@handle, channel link, video link or name", autocapitalize: "none" })
  const find = el("button", { class: "btn primary", type: "submit", text: "Find" })
  const form = el("form", { class: "row" }, q, find)
  form.onsubmit = (e) => { e.preventDefault(); bstate.q = q.value.trim(); bstate.channel = ""; bstate.page = ""; bstate.stack = []; sessionStorage.setItem("browseQ", bstate.q); sessionStorage.removeItem("browseCh"); render() }
  const campaignId = +(sessionStorage.getItem("browseCampaign") || 0)
  view.append(el("h1", { text: "Browse YouTube" }),
    campaignId ? el("div", { class: "card row small" }, el("span", { class: "grow", text: "🎯 Videos you clip here count for the campaign (its hashtags go into the captions)." }),
      el("button", { class: "btn sm", type: "button", text: "Stop", onclick: () => { sessionStorage.removeItem("browseCampaign"); render() } })) : null,
    el("div", { class: "card col" }, form,
    el("p", { class: "hint", style: { margin: 0 }, text: "Each page costs ~3 of 10,000 free YouTube units a day; a name search costs 100." })))
  if (!bstate.q && !bstate.channel) return
  const params = new URLSearchParams(bstate.channel ? { channel: bstate.channel, page: bstate.page } : { q: bstate.q })
  const r = await api(`/browse/youtube?${params}`)
  if (r.choices) {
    if (!r.choices.length) { view.append(el("div", { class: "empty" }, el("b", { text: "🤷" }), "No channel found.")); return }
    view.append(el("h2", { text: "Which channel?" }), r.choices.map((c) => el("div", { class: "card row", style: { cursor: "pointer" }, onclick: () => { bstate.channel = c.id; sessionStorage.setItem("browseCh", c.id); render() } },
      el("img", { src: c.thumb, alt: "", style: { width: "48px", height: "48px", borderRadius: "50%" } }),
      el("div", { class: "grow" }, el("b", { text: c.title }), el("div", { class: "small muted ellipsis", text: c.description })))))
    return
  }
  const ch = r.channel
  bstate.channel = ch.id
  sessionStorage.setItem("browseCh", ch.id)
  const hide = el("input", { type: "checkbox", checked: bstate.hideShorts })
  hide.onchange = () => { bstate.hideShorts = hide.checked; sessionStorage.setItem("hideShorts", hide.checked ? "1" : "0"); render() }
  view.append(el("div", { class: "card row" },
    el("img", { src: ch.thumb, alt: "", style: { width: "56px", height: "56px", borderRadius: "50%" } }),
    el("div", { class: "grow" }, el("b", { text: ch.title }), el("div", { class: "small muted", text: `${ch.handle ? "@" + ch.handle + " · " : ""}${nf.format(ch.subscribers)} subscribers · ${nf.format(ch.video_count)} videos` }),
      r.watched ? el("span", { class: "chip good", text: `📡 watched (${r.watched.permission})` }) : null),
    el("a", { class: "btn sm", href: ch.url, target: "_blank", rel: "noopener", text: "Open" })),
    el("label", { class: "switch", style: { margin: "4px 0 10px" } }, hide, "Hide Shorts and videos under 3 minutes"))
  const vids = r.videos.filter((v) => !(bstate.hideShorts && v.duration < 180))
  if (!vids.length) view.append(el("div", { class: "empty" }, "No long videos on this page."))
  for (const v of vids) {
    const live = v.state === "live" ? "🔴 LIVE" : v.state === "upcoming" ? "⏰ upcoming" : ""
    const action = v.added
      ? el("a", { class: "btn sm", href: `#source/${v.added.id}`, text: `✓ Added · ${(STATUS[v.added.status] || [v.added.status])[0]}` })
      : live ? el("span", { class: "chip", text: live })
        : el("button", { class: "btn sm primary", type: "button", text: "✂️ Clip this", onclick: () => clipThisSheet(v, ch, r.watched, render) })
    view.append(el("div", { class: "card", style: { display: "grid", gridTemplateColumns: "140px 1fr", gap: "12px", padding: "10px" } },
      el("a", { href: v.url, target: "_blank", rel: "noopener", style: { position: "relative", display: "block" } },
        el("img", { src: v.thumb, alt: "", loading: "lazy", style: { width: "140px", aspectRatio: "16/9", objectFit: "cover", borderRadius: "8px", display: "block" } }),
        el("span", { class: "chip", style: { position: "absolute", right: "4px", bottom: "4px", background: "rgba(0,0,0,.75)", color: "#fff" }, text: mmss(v.duration) })),
      el("div", { class: "col", style: { gap: "4px", minWidth: 0 } },
        el("b", { style: { fontSize: "14px", lineHeight: 1.3 }, text: v.title }),
        el("div", { class: "small muted", text: `${new Date(v.published).toLocaleDateString()} · ${nf.format(v.views)} views` }),
        el("div", {}, action))))
  }
  const prev = el("button", { class: "btn", type: "button", text: "← Newer", disabled: !bstate.stack.length })
  prev.onclick = () => { bstate.page = bstate.stack.pop() || ""; render(); scrollTo(0, 0) }
  const next = el("button", { class: "btn", type: "button", text: "Older →", disabled: !r.next })
  next.onclick = () => { bstate.stack.push(bstate.page); bstate.page = r.next; render(); scrollTo(0, 0) }
  view.append(el("div", { class: "row", style: { justifyContent: "space-between", marginTop: "8px" } }, prev,
    el("span", { class: "small muted", text: `Page ${bstate.stack.length + 1}${r.total ? ` of ${Math.ceil(r.total / 24)}` : ""}` }), next))
}

// ---------------------------------------------------------------------------------------------------------
// Campaigns
// ---------------------------------------------------------------------------------------------------------
const FLAG = {
  needs_audio: ["🎵 needs a song/audio — breaks your music rule", "bad"], custom_edit: ["✂️ needs a custom edit (required text/segment)", "warn"],
  budget_low: ["💸 budget almost used up", "warn"], other_platforms: ["📵 not your platforms", "bad"], product_cart: ["🛒 product cart", ""],
}
const rp = (n) => "Rp" + new Intl.NumberFormat("id-ID").format(n || 0)

async function campaignsPage(view) {
  const show = sessionStorage.getItem("campShow") || "open"
  const r = await api(`/campaigns?show=${show}`)
  const refresh = el("button", { class: "btn sm", type: "button", text: "↻ Refresh" })
  refresh.onclick = () => busy(refresh, async () => { const x = await api("/campaigns/refresh", { method: "POST" }); toast(x.new ? `${x.new} new campaign(s)` : "Up to date"); render() }).catch(() => {})
  view.append(el("div", { class: "topbar" }, el("h1", { text: "Campaigns" }), refresh),
    el("div", { class: "seg" }, [["open", "Open"], ["joined", "Joined"], ["ended", "Ended"], ["hidden", "Hidden"], ["all", "All"]].map(([k, l]) =>
      el("button", { class: `btn sm ${k === show ? "on" : ""}`, type: "button", text: l, onclick: () => { sessionStorage.setItem("campShow", k); render() } }))),
    el("p", { class: "hint", style: { marginTop: 0 }, text: `Clippo's public list, checked every 3 hours${r.refreshed ? ` (last ${ago(r.refreshed)})` : ""}. Join in Clippo's app; Clip Studio does the clipping, hashtags and the links to submit.` }))
  if (!r.campaigns.length) { view.append(el("div", { class: "empty" }, el("b", { text: "🎯" }), show === "open" ? "No campaigns yet — tap Refresh." : "Nothing here.")); return }
  for (const c of r.campaigns) {
    const card = el("div", { class: "card", style: { cursor: "pointer" }, onclick: () => campaignSheet(c) },
      el("div", { class: "row", style: { alignItems: "flex-start" } },
        c.image ? el("img", { src: c.image, alt: "", loading: "lazy", style: { width: "56px", height: "56px", borderRadius: "10px", objectFit: "cover" } }) : null,
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("b", { text: c.title }),
          el("div", { class: "small muted", text: `${c.creator} · ${c.platform}` }),
          el("div", { class: "row wrap", style: { gap: "5px", marginTop: "6px" } },
            el("span", { class: "chip accent", text: `${rp(c.rate)} / 1k views` }),
            c.platforms.map((p) => el("span", { class: "chip", text: p })),
            c.joined ? el("span", { class: "chip good", text: "✓ joined" }) : null,
            c.status === "ended" ? el("span", { class: "chip", text: "ended" }) : null,
            c.to_submit ? el("span", { class: "chip warn", text: `${c.to_submit} link(s) to submit` }) : null),
          el("div", { class: "progress" }, el("i", { style: { width: `${Math.min(100, c.budget_used)}%`, background: c.budget_used >= 90 ? "var(--bad)" : "var(--accent)" } })),
          el("div", { class: "small muted", style: { marginTop: "3px" }, text: `budget used ${Math.round(c.budget_used)}% · ${c.clippers.toLocaleString()} clippers${c.sources ? ` · ${c.sources} video(s), ${c.clips} clip(s) yours` : ""}` }),
          c.flags.length ? el("div", { class: "row wrap", style: { gap: "5px", marginTop: "6px" } }, c.flags.map((f) => el("span", { class: `chip ${(FLAG[f] || ["", ""])[1]}`, text: (FLAG[f] || [f])[0] }))) : null)))
    view.append(card)
  }
}

async function campaignSheet(c) {
  const { body, close } = sheet(c.title, { onClose: render })
  const yt = c.footage.filter((f) => /youtube\.com\/watch|youtu\.be\/|youtube\.com\/shorts\//.test(f.url))
  const joined = el("input", { type: "checkbox", checked: !!c.joined })
  joined.onchange = async () => { try { await api(`/campaigns/${c.id}`, { method: "PATCH", json: { joined: joined.checked } }); toast(joined.checked ? "Marked as joined" : "Unmarked") } catch (e) { toast(e.message, "bad") } }
  const clipBtn = el("button", { class: "btn primary", type: "button", text: `✂️ Clip the YouTube footage (${yt.length})`, disabled: !yt.length })
  clipBtn.onclick = () => busy(clipBtn, async () => {
    const x = await api(`/campaigns/${c.id}/clip`, { method: "POST" })
    toast(x.added.length ? `${x.added.length} video(s) added — clips in ~10 min` : "Already added")
  }).catch(() => {})
  const upload = el("button", { class: "btn", type: "button", text: "⬆️ Upload campaign footage", onclick: () => { sessionStorage.setItem("campaignForUpload", c.id); close(); location.hash = "sources" } })
  const tags = c.hashtags.join(" ")
  const notes = el("textarea", { value: c.notes, placeholder: "Your notes (e.g. what the brief asks)" })
  notes.onchange = async () => { try { await api(`/campaigns/${c.id}`, { method: "PATCH", json: { notes: notes.value } }); toast("Saved") } catch (e) { toast(e.message, "bad") } }
  const hide = el("button", { class: "btn ghost", type: "button", text: c.hidden ? "Show again" : "Hide this campaign", onclick: async () => { await api(`/campaigns/${c.id}`, { method: "PATCH", json: { hidden: !c.hidden } }); close() } })
  const sub = el("div", { class: "col" }, el("div", { class: "muted small", text: "Loading…" }))
  body.append(
    c.image ? el("img", { src: c.image, alt: "", style: { width: "100%", maxHeight: "220px", objectFit: "cover", borderRadius: "12px", marginBottom: "10px" } }) : null,
    el("div", { class: "row wrap", style: { gap: "5px" } }, el("span", { class: "chip accent", text: `${rp(c.rate)} / 1k views` }), c.platforms.map((p) => el("span", { class: "chip", text: p })),
      el("span", { class: "chip", text: `budget used ${Math.round(c.budget_used)}%` }), el("span", { class: "chip", text: `${c.clippers.toLocaleString()} clippers` })),
    c.flags.length ? el("div", { class: "col", style: { gap: "4px", margin: "10px 0" } }, c.flags.map((f) => el("span", { class: `chip ${(FLAG[f] || ["", ""])[1]}`, style: { alignSelf: "flex-start" }, text: (FLAG[f] || [f])[0] }))) : null,
    el("div", { class: "btns" }, el("a", { class: "btn primary", href: c.url, target: "_blank", rel: "noopener", text: "Join in Clippo ↗" })),
    el("label", { class: "switch", style: { margin: "10px 0" } }, joined, "I joined this campaign"),
    el("h2", { text: "Brief" }), el("pre", { class: "caption", text: c.brief || "(no brief)" }),
    el("h2", { text: "Required hashtags" }),
    el("div", { class: "row" }, el("div", { class: "grow small", text: tags || "none" }), tags ? el("button", { class: "btn sm", type: "button", text: "Copy", onclick: async () => { try { await navigator.clipboard.writeText(tags); toast("Copied") } catch (_) {} } }) : null),
    el("p", { class: "hint", text: "Added automatically to every caption of clips made for this campaign." }),
    el("h2", { text: `Footage (${c.footage.length})` }),
    el("div", { class: "card small" }, c.footage.map((f) => el("div", { style: { padding: "6px 0", borderBottom: "1px solid var(--line)" } },
      f.required ? el("span", { class: "chip warn", text: "required" }) : null, " ", el("a", { href: f.url, target: "_blank", rel: "noopener", text: f.url.replace(/^https?:\/\//, "").slice(0, 60) }),
      f.text ? el("div", { class: "muted", text: f.text }) : null,
      yt.includes(f) ? el("div", { class: "muted", text: "▶ YouTube — Clip Studio fetches it" })
        : /youtube\.com\/(@|channel\/|c\/|user\/)/.test(f.url) ? el("a", { class: "btn sm", href: "#browse", text: "🔎 Browse this channel", onclick: () => { sessionStorage.setItem("browseQ", f.url); sessionStorage.removeItem("browseCh"); sessionStorage.setItem("browseCampaign", c.id); bstate.q = f.url; bstate.channel = ""; bstate.page = ""; bstate.stack = [] } })
          : el("div", { class: "muted", text: "Download it yourself, then Upload" })))),
    el("div", { class: "btns" }, clipBtn, upload),
    el("h2", { text: "Ready to submit" }), sub,
    el("h2", { text: "Notes" }), notes, el("div", { class: "btns" }, hide))
  const { items } = await api(`/campaigns/${c.id}/submit`)
  const pending = items.filter((i) => !i.submitted)
  sub.replaceChildren(items.length ? [
    ...items.map((i) => el("div", { class: "row small", style: { borderBottom: "1px solid var(--line)", padding: "5px 0" } },
      el("span", { class: "grow ellipsis" }, `${i.submitted ? "✓ " : ""}${i.platform}: `, el("a", { href: i.url, target: "_blank", rel: "noopener", text: i.url.replace(/^https?:\/\//, "").slice(0, 45) })))),
    pending.length ? el("div", { class: "btns" },
      el("button", { class: "btn", type: "button", text: `Copy ${pending.length} link(s)`, onclick: async () => { try { await navigator.clipboard.writeText(pending.map((i) => i.url).join("\n")); toast("Copied — paste them in Clippo") } catch (_) {} } }),
      el("button", { class: "btn good", type: "button", text: "Mark submitted", onclick: async (e) => { await busy(e.target, () => api(`/campaigns/${c.id}/submitted`, { method: "POST", json: { items: pending.map((i) => ({ clip_id: i.clip_id, platform: i.platform })) } })).catch(() => {}); close() } })) : el("div", { class: "muted small", text: "All submitted." }),
  ] : el("div", { class: "muted small", text: "Posted clips of this campaign appear here with their links (TikTok: use Mark posted with the link after you post)." }))
}

// ---------------------------------------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------------------------------------
async function statsPage(view) {
  const days = +(sessionStorage.getItem("statsDays") || 30)
  const r = await api(`/stats?days=${days}`)
  const names = { instagram: "Instagram", tiktok: "TikTok", youtube: "YouTube" }
  view.append(el("h1", { text: "Stats" }),
    el("div", { class: "seg" }, [7, 30, 90].map((d) => el("button", { class: `btn sm ${d === days ? "on" : ""}`, type: "button", text: `${d} days`, onclick: () => { sessionStorage.setItem("statsDays", d); render() } }))),
    el("div", { class: "card" }, el("dl", { class: "kv" },
      el("dt", { text: "Views" }), el("dd", { text: r.views.toLocaleString() }),
      ...Object.entries(r.totals).flatMap(([k, v]) => [el("dt", { text: names[k] }), el("dd", { text: `${v.posts} posts · ${v.views.toLocaleString()} views` })]),
      el("dt", { text: "Scheduled" }), el("dd", { text: r.queue.clips ? `${r.queue.clips} clips until ${new Date(r.queue.until * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "queue empty" }),
      el("dt", { text: "Waiting for you" }), el("dd", {}, r.review_waiting ? el("a", { href: "#review", text: `${r.review_waiting} to review` }) : "none"))),
    r.ig_insights ? null : el("p", { class: "hint", text: "Instagram views need the 'instagram_business_manage_insights' permission on your Meta app (until then: likes and comments). TikTok and YouTube views: type them in below." }))
  if (!r.clips.length) { view.append(el("div", { class: "empty" }, el("b", { text: "📊" }), "No posts in this period yet.")); return }
  view.append(el("h2", { text: "Clips, most viewed first" }))
  for (const c of r.clips) {
    view.append(el("div", { class: "card" },
      el("div", { class: "row" }, el("b", { class: "grow", text: c.hook }), el("span", { class: "chip accent", text: `👁 ${c.views.toLocaleString()}` })),
      el("div", { class: "small muted", text: `${c.creator} · ${new Date(c.posted_at * 1000).toLocaleDateString()} · score ${Math.round(c.score)}` }),
      el("div", { class: "row wrap", style: { marginTop: "6px", gap: "6px" } }, Object.entries(c.platforms).map(([p, v]) => {
        const inp = el("input", { type: "number", min: 0, value: v.views || "", placeholder: "views", style: { width: "110px", padding: "4px 8px" } })
        inp.onchange = async () => { try { await api(`/clips/${c.clip_id}/views`, { method: "PUT", json: { platform: p, views: +inp.value || 0 } }); toast("Saved") } catch (e) { toast(e.message, "bad") } }
        const auto = p === "instagram" && !v.manual
        return el("span", { class: "chip", style: { padding: "4px 10px" } }, `${names[p]}: `,
          auto ? `${(v.views || 0).toLocaleString()} views${v.likes != null ? ` · ❤ ${v.likes}` : ""}${v.comments != null ? ` · 💬 ${v.comments}` : ""}` : inp,
          v.url ? el("a", { href: v.url, target: "_blank", rel: "noopener", text: " ↗" }) : null)
      }))))
  }
  view.append(el("h2", { text: "By creator" }), el("div", { class: "card" }, el("dl", { class: "kv" },
    r.creators.flatMap((c) => [el("dt", { text: c.creator }), el("dd", { text: `${c.posts} posts · ${c.views.toLocaleString()} views` })]))))
}

// ---------------------------------------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------------------------------------
async function channelsPage(view) {
  const { channels, configured } = await api("/channels")
  const platform = el("select", {}, ["youtube", "twitch", "kick"].map((p) => el("option", { value: p, text: { youtube: "YouTube", twitch: "Twitch", kick: "Kick" }[p] })))
  const link = el("input", { placeholder: "@handle or channel link" })
  const perm = permSelect()
  const proof = el("input", { placeholder: "Proof: where the creator allows clipping / campaign link" })
  const cname = el("input", { placeholder: "Campaign name (optional)" })
  const crate = el("input", { placeholder: "Rate, e.g. $1 / 1k views" })
  const curl = el("input", { placeholder: "Campaign rules link" })
  const up = el("input", { type: "checkbox", checked: true })
  const lv = el("input", { type: "checkbox", checked: true })
  const minm = el("input", { type: "number", value: 5, min: 0, max: 600, inputmode: "numeric" })
  const warn = el("div", { class: "small", style: { color: "var(--warn)" } })
  const upLabel = el("label", { class: "switch" }, up, "Clip new uploads")
  const sync = () => {
    const p = platform.value
    warn.textContent = configured[p] ? "" : `${p === "youtube" ? "YouTube" : p === "twitch" ? "Twitch" : "Kick"} keys aren't set up on the server yet, so this platform can't be watched.`
    upLabel.hidden = p === "kick"
  }
  platform.onchange = sync
  sync()
  const add = el("button", { class: "btn primary", type: "button", text: "Add channel" })
  add.onclick = () => busy(add, async () => {
    if (!perm.value) throw new Error("Choose the permission first.")
    await api("/channels", { method: "POST", json: { platform: platform.value, link: link.value.trim(), permission: perm.value, proof: proof.value,
      campaign_name: cname.value, campaign_rate: crate.value, campaign_url: curl.value, watch_uploads: up.checked, watch_live: lv.checked, min_minutes: +minm.value || 0 } })
    toast("Channel added — watching from now on"); render()
  }).catch(() => {})
  view.append(el("h1", { text: "Channels" }))
  if (!channels.length) view.append(el("p", { class: "muted", text: "Watched channels: new uploads and live streams are clipped automatically (only videos published after you add the channel)." }))
  for (const c of channels) {
    const en = el("input", { type: "checkbox", checked: !!c.enabled })
    en.onchange = async () => { try { await api(`/channels/${c.id}`, { method: "PATCH", json: { enabled: en.checked } }); toast(en.checked ? "Watching" : "Paused") } catch (e) { toast(e.message, "bad") } }
    const camp = c.campaign && c.campaign.name ? `${c.campaign.name}${c.campaign.rate ? ` (${c.campaign.rate})` : ""}` : ""
    view.append(el("div", { class: "card" },
      el("div", { class: "row" }, el("span", { text: PLATFORM_ICON[c.platform] }), el("b", { class: "grow ellipsis", text: c.title }),
        c.live_now ? el("span", { class: "chip bad", text: "LIVE" }) : null, el("label", { class: "switch" }, en)),
      el("div", { class: "small muted", style: { marginTop: "4px" } }, `${c.permission}${camp ? ` · ${camp}` : ""} · ${[c.watch_uploads ? "uploads" : "", c.watch_live ? "live" : ""].filter(Boolean).join(" + ") || "nothing"} · min ${c.min_minutes} min`),
      el("div", { class: "small muted" }, c.push ? "⚡ instant notifications on" : "polling", c.last_checked ? ` · checked ${ago(c.last_checked)}` : ""),
      c.last_error ? el("div", { class: "small", style: { color: "var(--bad)" }, text: c.last_error }) : null,
      el("div", { class: "row", style: { marginTop: "8px", justifyContent: "flex-end" } },
        el("a", { class: "btn sm", href: c.url, target: "_blank", rel: "noopener", text: "Open" }),
        el("button", { class: "btn sm bad", type: "button", text: "Remove", onclick: async (e) => { if (!confirm(`Stop watching ${c.title}?`)) return; await busy(e.target, () => api(`/channels/${c.id}`, { method: "DELETE" })).catch(() => {}); render() } }))))
  }
  view.append(el("div", { class: "card col" }, el("b", { text: "Add a channel" }), platform, link, perm, proof, warn,
    el("details", {}, el("summary", { class: "small", style: { cursor: "pointer", fontWeight: 650 }, text: "Campaign details (optional)" }), el("div", { class: "col", style: { marginTop: "10px" } }, cname, crate, curl)),
    upLabel, el("label", { class: "switch" }, lv, "Record live streams (max 4 h, one at a time)"),
    el("label", { class: "field" }, "Skip videos shorter than (minutes)", minm), add))
}

// ---------------------------------------------------------------------------------------------------------
// More: notifications, status, settings
// ---------------------------------------------------------------------------------------------------------
async function enablePush(btn) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) { toast("Add Clip Studio to the Home Screen first (Share → Add to Home Screen).", "bad"); return }
  await busy(btn, async () => {
    const perm = await Notification.requestPermission()
    if (perm !== "granted") throw new Error("Notifications are blocked in the phone settings.")
    const reg = await navigator.serviceWorker.ready
    const { key } = await api("/push/key")
    const raw = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((key.length + 3) % 4)), (c) => c.charCodeAt(0))
    let sub = await reg.pushManager.getSubscription()
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: raw })
    const j = sub.toJSON()
    await api("/push/subscribe", { method: "POST", json: { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth, ua: navigator.userAgent.slice(0, 300) } })
    const r = await api("/push/test", { method: "POST" })
    toast(r.sent ? "Notifications on 🔔" : "Registered")
  }).catch(() => {})
}

async function morePage(view) {
  const [st, { settings }] = await Promise.all([api("/status"), api("/settings")])
  const pushBtn = el("button", { class: "btn primary", type: "button", text: "🔔 Turn on notifications" })
  pushBtn.onclick = () => enablePush(pushBtn)
  const u = st.usage
  const keys = Object.entries(st.keys).map(([k, v]) => el("span", { class: `chip ${v ? "good" : ""}`, text: `${v ? "✓" : "–"} ${k}` }))
  view.append(el("h1", { text: "More" }), el("a", { class: "btn", href: "#channels", style: { width: "100%", marginBottom: "12px" }, text: "📡 Watched channels" }), el("div", { class: "card col" }, pushBtn,
    el("p", { class: "hint", style: { margin: 0 }, text: "On iPhone: open this site in Safari → Share → Add to Home Screen, open it from the Home Screen, then tap the button." })))
  const { accounts } = await api("/accounts")
  const names = { instagram: "Instagram Reels", tiktok: "TikTok", youtube: "YouTube Shorts" }
  view.append(el("h2", { text: "Auto-posting" }), el("div", { class: "card col" },
    el("p", { class: "hint", style: { margin: 0 }, text: "After you approve a clip and its full-quality video is ready, it's posted to the platforms switched on here." }),
    Object.entries(accounts).map(([k, a]) => {
      const t = el("input", { type: "checkbox", checked: a.autopost, disabled: !a.connected })
      t.onchange = async () => { try { await api("/accounts/autopost", { method: "PUT", json: { platform: k, on: t.checked } }); toast(t.checked ? `${names[k]}: auto-post on` : `${names[k]}: auto-post off`) } catch (e) { toast(e.message, "bad") } }
      let action = null
      if (k === "tiktok" && a.can_connect) {
        action = a.connected
          ? el("button", { class: "btn sm", type: "button", text: "Disconnect", onclick: async (e) => { if (!confirm("Disconnect TikTok?")) return; await busy(e.target, () => api("/tiktok/disconnect", { method: "POST" })).catch(() => {}); render() } })
          : el("a", { class: "btn sm primary", href: "/api/tiktok/connect", text: "Connect TikTok" })
      }
      return el("div", { class: "row wrap", style: { padding: "6px 0", borderTop: "1px solid var(--line)" } },
        el("div", { class: "grow" }, el("b", { text: names[k] }),
          el("div", { class: "small muted", text: a.connected ? `Connected${a.username ? ` as ${k === "tiktok" ? "" : "@"}${a.username}` : ""}${a.expires ? " · token renews itself" : ""}` : "Not connected" }),
          el("div", { class: "small muted", text: a.note || "" }),
          a.error ? el("div", { class: "small", style: { color: "var(--bad)" }, text: a.error }) : null),
        action, el("label", { class: "switch" }, t))
    })))
  view.append(el("h2", { text: "Status" }), el("div", { class: "card" },
    el("dl", { class: "kv" },
      el("dt", { text: "Worker" }), el("dd", { text: st.worker_alive ? "🟢 running" : "🔴 not running" }),
      el("dt", { text: "Working on" }), el("dd", { text: st.running.length ? st.running.map((r) => `${r.kind}: ${r.title || "clip " + r.clip_id}${r.step ? ` (${r.step})` : ""}`).join("; ") : "nothing" }),
      el("dt", { text: "Queue" }), el("dd", { text: Object.entries(st.queue).map(([k, n]) => `${n} ${k}`).join(", ") || "empty" }),
      el("dt", { text: "Free disk" }), el("dd", { text: `${st.disk_free_gb} GB (stops below ${st.disk_min_gb})` }),
      el("dt", { text: "Groq today" }), el("dd", { text: `${Math.round(u.groq_seconds / 60)} / ${Math.round(u.groq_limit / 60)} min audio` }),
      el("dt", { text: "Gemini today" }), el("dd", { text: `${u.gemini_calls} calls${u.gemini_failures ? `, ${u.gemini_failures} busy` : ""}` }),
      el("dt", { text: "YouTube API" }), el("dd", { text: `${u.yt_units} / ${u.yt_limit} units` })),
    el("div", { class: "row wrap", style: { marginTop: "10px", gap: "5px" } }, keys)))
  const f = {}
  const num = (k, label, hint) => { f[k] = el("input", { type: "number", value: settings[k], inputmode: "numeric" }); return el("label", { class: "field" }, label, f[k], hint ? el("span", { class: "hint", text: hint }) : null) }
  f.music_allowed = el("select", {}, el("option", { value: "none", text: "No music at all (strict)", selected: settings.music_allowed === "none" }), el("option", { value: "faint", text: "Allow faint background music", selected: settings.music_allowed === "faint" }))
  f.hashtags = el("input", { value: settings.hashtags })
  f.schedule_on = el("input", { type: "checkbox", checked: settings.schedule_on })
  f.post_times = el("input", { value: settings.post_times, placeholder: "12:00, 18:00, 21:00" })
  f.reminder_time = el("input", { type: "time", value: settings.reminder_time })
  f.weekly_summary = el("input", { type: "checkbox", checked: settings.weekly_summary })
  f.caption_template = el("textarea", { value: settings.caption_template })
  f.keyword_blocklist = el("textarea", { value: settings.keyword_blocklist })
  const save = el("button", { class: "btn primary", type: "button", text: "Save settings" })
  save.onclick = () => busy(save, async () => {
    const body = {}
    for (const [k, inp] of Object.entries(f)) body[k] = inp.type === "number" ? +inp.value : inp.type === "checkbox" ? inp.checked : inp.value
    await api("/settings", { method: "PUT", json: body }); toast("Saved")
  }).catch(() => {})
  view.append(el("h2", { text: "Settings" }), el("div", { class: "card col" },
    el("b", { text: "Posting schedule" }),
    el("label", { class: "switch" }, f.schedule_on, "Post approved clips on a schedule (off = right away)"),
    el("label", { class: "field" }, "Posting times (WIB), one clip per time per platform", f.post_times, el("span", { class: "hint", text: "e.g. 12:00, 18:00, 21:00 = 3 posts a day. Approve a batch and it posts all week." })),
    el("label", { class: "field" }, "Daily reminder at", f.reminder_time, el("span", { class: "hint", text: "Push when clips wait for review or the queue runs low." })),
    el("label", { class: "switch" }, f.weekly_summary, "Weekly summary on Monday 09:00"),
    el("b", { text: "Clips", style: { marginTop: "8px" } }),
    num("clips_per_hour", "Clips per hour of video (max)"), num("min_clip_seconds", "Shortest clip (s)"), num("max_clip_seconds", "Longest clip (s)"),
    num("score_threshold", "Score threshold", "Higher = fewer, better clips. Gemini's 1–10 rating × 8, plus loudness and laughter bonuses."),
    el("label", { class: "field" }, "Music", f.music_allowed, el("span", { class: "hint", text: "Platforms mute or claim clips with music." })),
    num("max_source_hours", "Longest video / live recording (hours)"), num("auto_max_age_hours", "Watched channels: ignore videos older than (hours)"),
    el("label", { class: "field" }, "Always-added hashtags", f.hashtags),
    el("label", { class: "field" }, "Caption template", f.caption_template, el("span", { class: "hint", text: "Use {hook} {caption} {creator} {source_url} {hashtags}" })),
    el("label", { class: "field" }, "Refuse titles containing (comma separated)", f.keyword_blocklist), save))
  view.append(el("h2", { text: "Activity" }), el("div", { class: "card log" }, st.events.length ? st.events.map((e) =>
    el("div", {}, el("span", { class: "muted", text: `${ago(e.at)} · ` }), el("span", { style: { color: e.level === "error" ? "var(--bad)" : e.level === "warn" ? "var(--warn)" : "inherit" }, text: e.message }))) : el("div", { class: "muted", text: "Nothing yet." })))
  view.append(el("div", { class: "btns", style: { marginTop: "16px" } }, el("button", { class: "btn ghost", type: "button", text: `Log out (${me.username})`, onclick: async () => { await api("/logout", { method: "POST" }); me = null; render() } })))
}

render()
