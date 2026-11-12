// Swarmify's service worker: the app shell only. vite.config.ts fills in the build's file list and
// emits this as /sw.js. The API, its event streams, media and downloads always go to the network:
// they are never matched, cached or served from here.
const SHELL = __PRECACHE__;
const CACHE = `swarmify-shell-${__VERSION__}`;

self.addEventListener("install", (event) => {
  // A new version waits until the page asks for it (SKIP_WAITING), so it never swaps mid-form.
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/mcp") return;
  if (request.mode === "navigate") {
    // Network first, so a deploy shows at once; the cached shell only when offline.
    event.respondWith(fetch(request).catch(() => caches.match("/index.html")));
    return;
  }
  if (SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((hit) => hit ?? fetch(request)));
  }
});
