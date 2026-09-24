/*
 * uc-hub service worker: keeps the app shell available offline.
 *
 * - Navigations: network first, falling back to the cached index.html.
 * - Same-origin static files (hashed /assets/*, icon, manifest): cache first.
 * - /api is never cached or intercepted: live data and SSE must always come
 *   from the gateway, and a stale approval must never be shown as current.
 */
const CACHE = "uc-hub-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put("/index.html", response.clone());
    return response;
  } catch (err) {
    const cached = (await cache.match("/index.html")) || (await cache.match("/"));
    if (cached) return cached;
    throw err;
  }
}

// Vite names build files <name>-<8 character hash>.<ext>.
const HASHED = /^(.*\/[^/]+)-[A-Za-z0-9_-]{8}(\.[a-z0-9]+)$/;

/** A new build of a file replaces the older builds of the same file, so the cache does not grow per deployment. */
async function pruneOlderBuilds(cache, pathname) {
  const m = HASHED.exec(pathname);
  if (!m) return;
  for (const key of await cache.keys()) {
    const other = new URL(key.url).pathname;
    const o = HASHED.exec(other);
    if (other !== pathname && o && o[1] === m[1] && o[2] === m[2]) await cache.delete(key);
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok && response.type === "basic") {
    await cache.put(request, response.clone());
    await pruneOlderBuilds(cache, new URL(request.url).pathname);
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/api") return;
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }
  if (url.pathname.startsWith("/assets/") || SHELL.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
});
