// Shared helpers: DOM, API, sheets, toasts (same approach as the other household apps).

export const $ = (s, r = document) => r.querySelector(s)
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag)
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue
    if (k === "class") n.className = v
    else if (k === "text") n.textContent = v
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v)
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v)
    else if (k === "value") n.value = v
    else if (k === "checked") n.checked = !!v
    else n.setAttribute(k, v === true ? "" : v)
  }
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)))
  return n
}

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status }
}
let onAuth = () => {}
export const setAuthHandler = (fn) => { onAuth = fn }

export async function api(path, { method = "GET", json, quiet = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: { "X-CS": "1" } }
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json) }
  let res
  for (let i = 0; ; i++) {
    try { res = await fetch("/api" + path, opts); break } catch (e) {
      // iPhone PWAs often lose the first request after waking up: retry GETs.
      if (method !== "GET" || i >= 2) throw new ApiError(0, "No connection. Check your internet.")
      await sleep(600 * (i + 1))
    }
  }
  let data = {}
  try { data = await res.json() } catch (_) {}
  if (res.status === 401 && !quiet) onAuth()
  if (!res.ok) throw new ApiError(res.status, data.error || `Error ${res.status}`)
  return data
}

let toastT
export function toast(msg, kind = "") {
  const t = $("#toast")
  t.textContent = msg
  t.className = `toast ${kind}`
  t.hidden = false
  clearTimeout(toastT)
  toastT = setTimeout(() => (t.hidden = true), 2800)
}

export function sheet(title, { onClose } = {}) {
  const body = el("div", { class: "sheet-body" })
  const close = el("button", { class: "btn ghost sm", type: "button", "aria-label": "Close", text: "✕" })
  const d = el("dialog", { class: "sheet" }, el("div", { class: "sheet-head" }, el("h2", { text: title }), close), body)
  const done = () => { if (d.open) d.close() }
  close.onclick = done
  d.addEventListener("click", (e) => { if (e.target === d) done() })
  d.addEventListener("close", () => { d.querySelectorAll("video").forEach((v) => v.pause()); d.remove(); onClose && onClose() })
  document.body.append(d)
  d.showModal()
  return { dialog: d, body, close: done }
}

export async function busy(btn, fn) {
  btn.disabled = true
  btn.classList.add("loading")
  try { return await fn() } catch (e) { toast(e.message, "bad"); throw e } finally { btn.disabled = false; btn.classList.remove("loading") }
}

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}
export function mmss(s) {
  s = Math.round(s || 0)
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`
}
export const gb = (b) => (b / 1e9).toFixed(b > 1e10 ? 0 : 1) + " GB"
export const mb = (b) => (b / 1e6).toFixed(0) + " MB"

// Line icons (24×24, stroke = currentColor).
const PATHS = {
  review: "M4 5h16v14H4zM9.5 9l5 3-5 3z", ready: "M20 7 10 17l-5-5", add: "M12 5v14M5 12h14",
  campaigns: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 12h.01",
  stats: "M4 20V10M10 20V4M16 20v-7M22 20H2", more: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  chev: "M6 9l6 6 6-6", pause: "M8 5v14M16 5v14", play: "M7 5l12 7-12 7z", check: "M5 12.5l4.5 4.5L19 7",
  copy: "M9 9h11v11H9zM5 15V4h11",
}
export function svgIcon(name, cls = "ico") {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg")
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", cls); s.setAttribute("aria-hidden", "true")
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", PATHS[name] || "")
  s.append(p)
  return s
}

// Copy with clear feedback: the button turns green with "✓ Copied" for a moment, plus a toast.
export async function copyText(btn, text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text)
  } catch (_) {
    toast("Copy failed — long-press the text to copy it", "bad")
    return false
  }
  if (btn) {
    if (!btn.dataset.label) btn.dataset.label = btn.textContent  // the real label, even after quick double taps
    btn.classList.remove("copied"); void btn.offsetWidth; btn.classList.add("copied")
    btn.textContent = "✓ Copied"
    clearTimeout(btn._copyT)
    btn._copyT = setTimeout(() => { btn.classList.remove("copied"); btn.textContent = btn.dataset.label }, 1600)
  }
  toast(`✓ ${what}`)
  return true
}
