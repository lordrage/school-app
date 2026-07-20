/* School app service worker */
const CACHE = "school-v5";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // Data: network first, fall back to last cached copy
  if (url.pathname.endsWith("school-data.json")) {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put("./school-data.json", copy));
          return r;
        })
        .catch(() => caches.match("./school-data.json"))
    );
    return;
  }

  // App shell (HTML): network first, cache only as offline fallback.
  // Cache-first here served stale code after every deploy — the app looked synced
  // (data is network-first) while running an old build.
  const isShell = e.request.mode === "navigate" ||
                  url.pathname.endsWith("/") ||
                  url.pathname.endsWith("index.html");
  if (isShell) {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("./index.html", copy)); }
          return r;
        })
        .catch(() => caches.match("./index.html", { ignoreSearch: true }))
    );
    return;
  }

  // Static assets (icons, manifest): cache first, refresh in background
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true }).then((hit) => {
      const net = fetch(e.request)
        .then((r) => {
          if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
          return r;
        })
        .catch(() => hit);
      return hit || net;
    })
  );
});
