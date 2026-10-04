// Clip Studio service worker: installable app shell + push notifications. /api is never cached.
const VERSION = "__VERSION__"
const CACHE = "clipstudio-" + VERSION

self.addEventListener("install", () => { self.skipWaiting() })
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()))
})
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url)
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")))
    return
  }
  e.respondWith(caches.open(CACHE).then(async (c) => {
    const hit = await c.match(e.request)
    if (hit) return hit
    const res = await fetch(e.request)
    if (res.ok) c.put(e.request, res.clone())
    return res
  }))
})
self.addEventListener("push", (e) => {
  let d = {}
  try { d = e.data ? e.data.json() : {} } catch (_) { d = { title: "Clip Studio", body: e.data && e.data.text() } }
  e.waitUntil(self.registration.showNotification(d.title || "Clip Studio", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, icon: "/icons/icon-192.png", badge: "/icons/icon-192.png",
    data: { url: d.url || "/" },
  }))
})
self.addEventListener("notificationclick", (e) => {
  e.notification.close()
  const url = (e.notification.data && e.notification.data.url) || "/"
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true })
    for (const c of all) { if ("focus" in c) { c.postMessage({ type: "navigate", url }); return c.focus() } }
    return self.clients.openWindow(url)
  })())
})
