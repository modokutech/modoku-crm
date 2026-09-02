// Modoku Hub service worker — KILL SWITCH.
//
// The PWA/offline-caching feature this service worker used to power has
// been reverted (it was causing layout problems for users on cached, stale
// CSS/JS). base.html no longer registers a service worker for new visits,
// but a browser that registered the OLD version of this file on an earlier
// visit will keep running it indefinitely — checking for updates on every
// navigation and re-installing whatever it finds at this URL — until it is
// explicitly told to stop.
//
// So instead of deleting this file/route, we keep serving it, but replace
// its contents with this tiny script: the next time an already-registered
// client checks for an update, it installs this version, which immediately
// unregisters itself and wipes every cache it created. After that one
// visit, the browser is back to normal (no service worker, no caching) and
// this file/route can eventually be removed for good.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
      await self.registration.unregister();
      const clientsList = await self.clients.matchAll({ type: "window" });
      clientsList.forEach((client) => client.navigate(client.url));
    })()
  );
});
