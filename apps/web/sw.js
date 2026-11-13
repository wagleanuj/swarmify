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

// Push notifications (issue 459): the server sends { title, body, url, tag, renotify }. The tag
// is one per gate or run, so a later push about it replaces the earlier one.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    // Not ours to read: still show something, as browsers require for every push.
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Swarmify", {
      body: data.body || "A run needs you.",
      tag: data.tag,
      renotify: Boolean(data.tag && data.renotify),
      icon: "/icon-192.png",
      badge: "/badge-96.png",
      data: { url: data.url || "/gates" },
    }),
  );
});

// A tap focuses an open Swarmify window and asks the app to route to the link itself (App.tsx),
// so the open app is not reloaded; with no window open it opens the link. Only paths on this
// origin are followed.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/gates", self.location.origin);
  const url = target.origin === self.location.origin ? target.href : `${self.location.origin}/gates`;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (!open) return self.clients.openWindow(url);
      await open.focus();
      const to = new URL(url);
      open.postMessage({ navigate: to.pathname + to.search + to.hash });
    })(),
  );
});
