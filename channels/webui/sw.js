const CACHE_NAME = 'openlumara-{{VERSION}}';

// Assets to precache (offline fallback only — while online the fetch handler
// below always goes to the network first)
const ASSETS_TO_CACHE = [
    {{FILE_LIST}},
    '/manifest.json',
    '/icon-192.png',
    '/icon-512.png',
];

console.log('Service Worker loaded: {{VERSION}}');

self.addEventListener('install', (event) => {
  console.log('Installing Service Worker...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log(`Caching ${ASSETS_TO_CACHE.length} assets...`);

        return Promise.all(
          ASSETS_TO_CACHE.map((url) => {
            return fetch(url).then((response) => {
              if (!response.ok) {
                console.error(`Failed to fetch ${url}: ${response.status}`);
                return null;
              }
              console.log(`Cached: ${url}`);
              return cache.put(url, response);
            }).catch((err) => {
              console.error(`Error fetching ${url}:`, err.message);
              return null;
            });
          })
        ).then(() => {
          console.log('Service Worker installed successfully');
        });
      })
      .catch((err) => {
        console.error('Installation failed:', err);
      })
  );
  // take over as soon as install finishes instead of waiting for the next
  // fresh navigation, so an update lands on the very next page load
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.log('Activating Service Worker...');
  event.waitUntil(
    self.clients.claim().then(() => {
      return caches.keys().then((cacheNames) => {
        console.log(`Found ${cacheNames.length} cache(s):`, cacheNames);

        const cachesToDelete = cacheNames.filter((name) => name !== CACHE_NAME);
        console.log(`Deleting ${cachesToDelete.length} old cache(s):`, cachesToDelete);

        return Promise.all(
          cachesToDelete.map((name) => caches.delete(name))
        );
      });
    })
    .then(() => {
      console.log('Service Worker activated');
    })
    .catch((err) => {
      console.error('Activation failed:', err);
    })
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // only handle same-origin GETs; anything else (POSTs, websockets,
  // cross-origin) passes through untouched
  if (request.method !== 'GET' || !request.url.startsWith(self.location.origin)) {
    return;
  }

  // network-first: always fetch fresh so a deploy's new CSS/JS reaches the
  // client immediately (the old cache-first strategy kept serving the first
  // cached copy — pre-deploy UI — indefinitely, which is how phones got stuck
  // on the old overflowing meeting/voice layout). the precached copy in
  // ASSETS_TO_CACHE remains the offline fallback
  event.respondWith(
    fetch(request)
      .then((response) => {
        // keep a fresh offline copy of asset-like responses; never cache
        // navigations (HTML must always come from the server)
        if (response.ok && (
          request.url.startsWith(self.location.origin + '/assets/') ||
          request.url.includes('/manifest.json') ||
          request.url.includes('/icon-')
        )) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch((err) => {
        // offline: fall back to whatever is cached
        return caches.match(request).then((cached) => cached || Response.error());
      })
  );
});
