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

const CACHE_NAME = "modoku-hub-shell-v1";

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

  // Static assets under /static/: cache-first for speed, refreshing the
  // cache in the background on every hit so an updated CSS/logo file
  // doesn't stay stale forever.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req)
          .then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
            }
            return res;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
