// Modoku Hub service worker.
//
// This is deliberately conservative for a CRM: every page is
// session-specific, carries a CSRF token, and shows live data (leads,
// invoices, claims...), so HTML pages are NEVER cached and NEVER served
// stale — every navigation always goes to the network. If the network is
// unreachable, the visitor gets a friendly offline page instead of the
// browser's default error screen.
//
// Only the static "app shell" assets (CSS, logo, icons, manifest) are
// cached, which is what makes repeat loads feel instant and lets the app
// icon/theme still resolve while offline.

// Bump this on every release that touches CSS/JS/static assets — it forces
// every visitor's browser onto a brand-new cache (the old one is deleted in
// the "activate" handler below) instead of quietly keeping whatever it
// cached the first time it ever loaded the app.
const CACHE_NAME = "modoku-hub-shell-v2";

const SHELL_ASSETS = [
  "/static/css/style.css",
  "/static/img/logo.png",
  "/static/img/favicon-16.png",
  "/static/img/favicon-32.png",
  "/static/img/favicon-180.png",
  "/static/img/icon-192.png",
  "/static/img/icon-512.png",
  "/static/manifest.json",
  "/static/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {
      // Never block install on a single missing/renamed asset.
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // never intercept POST/PUT/DELETE (forms, CSRF-protected actions)

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // let the browser handle CDN/font requests normally

  // Full-page navigations: always go to the network so logged-in users
  // always see live, current data — fall back to the offline page only if
  // the network request itself fails outright.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/static/offline.html"))
    );
    return;
  }

  // Static assets under /static/: network-first, falling back to the cache
  // only when the network is unreachable (offline).
  //
  // This used to be cache-first-with-background-refresh, which sounds
  // reasonable but has a real bug for an app that ships CSS/JS changes as
  // often as this one does: it always serves whatever was cached the very
  // first time a visitor loaded the app, and only refreshes the cache in
  // the background for the *next* visit — so a visitor who doesn't happen
  // to reopen the app again soon after a deploy can be stuck looking at an
  // old style.css indefinitely, with the page's HTML (always fresh, since
  // navigations are never cached) built for a newer version of that CSS.
  // That mismatch is exactly what breaks the layout after a style update.
  // Network-first means everyone gets the current CSS/JS whenever they're
  // online, and the cache is purely an offline fallback.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
  }
});
