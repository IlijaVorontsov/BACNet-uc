/*
 * uc-hub service worker: keeps the app shell available offline.
 *
 * - Install: caches index.html and the scripts and styles it loads. The page
 *   fetched them before this worker controlled it, so without this the app
 *   could not start offline until a second online visit.
 * - Navigations: network first, falling back to the cached index.html.
 * - Same-origin static files (hashed /assets/*, icon, manifest): cache first.
 * - /api is never cached or intercepted: live data and SSE must always come
 *   from the gateway, and a stale approval must never be shown as current.
 */
const CACHE = "uc-hub-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg"];
// Vite writes the entry's references as src="/assets/..." and href="/assets/...".
const ASSET_REF = /(?:src|href)="(\/assets\/[^"]+)"/g;

async function precache() {
  const cache = await caches.open(CACHE);
  // "reload" skips the HTTP cache: a stale index.html may name deleted builds.
  await cache.addAll(SHELL.map((path) => new Request(path, { cache: "reload" })));
  const index = await cache.match("/index.html");
  const html = index ? await index.text() : "";
  const assets = [...new Set(Array.from(html.matchAll(ASSET_REF), (m) => m[1]))];
  await cache.addAll(assets.map((path) => new Request(path, { cache: "reload" })));
  for (const path of assets) await pruneOlderBuilds(cache, path);
}

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
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
