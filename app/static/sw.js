// SpotwebNG service worker: caches only the static shell (CSS, JS, fonts, icons) so the app
// installs and starts fast. Pages, the API and every other response are private and dynamic:
// they are never cached and always go to the network.
const CACHE = "spotwebng-static-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(req).then((hit) =>
        hit || fetch(req).then((resp) => {
          if (resp.ok) cache.put(req, resp.clone());
          return resp;
        })
      )
    )
  );
});
