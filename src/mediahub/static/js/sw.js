// MediaHub service worker, served at /sw.js by web/routes/pwa.py, which fills in VERSION and
// PRECACHE. Pages and partials are never cached: they show live, signed-in data. Only
// versioned static assets (?v=<hash>, so immutable) and the offline page are kept.
"use strict";

const VERSION = "__VERSION__";
const PRECACHE = __PRECACHE__;
const CACHE = `mediahub-${VERSION}`;
const OFFLINE_URL = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => key.startsWith("mediahub-") && key !== CACHE).map((key) => caches.delete(key)),
      );
      if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
      await self.clients.claim();
    })(),
  );
});

async function navigate(event) {
  try {
    return (await event.preloadResponse) || (await fetch(event.request));
  } catch (_) {
    return (await caches.match(OFFLINE_URL)) || Response.error();
  }
}

async function staticAsset(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(navigate(event));
  } else if (url.pathname.startsWith("/static/") && url.searchParams.has("v")) {
    event.respondWith(staticAsset(request));
  }
  // Everything else (htmx partials, images, health) goes straight to the network.
});
