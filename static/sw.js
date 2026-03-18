const CACHE_VERSION  = 'v4';
const CACHE_NAME     = `connectsafe-cache-${CACHE_VERSION}`;
const OFFLINE_URL    = '/';

const APP_SHELL = [
  '/',
  '/client',
  '/dashboard',
  '/device',
  '/static/manifest.json',
  '/static/style.css',
  '/static/script.js',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

// ─────────────────────────────────────────────
// INSTALL — pre-cache the app shell
// ─────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
          .then((cache) => cache.addAll(APP_SHELL))
          .then(() => self.skipWaiting())
  );
});

// ─────────────────────────────────────────────
// ACTIVATE — purge old caches
// ─────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
          .then((keys) =>
            Promise.all(
              keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
            )
          )
          .then(() => self.clients.claim())
  );
});

// ─────────────────────────────────────────────
// FETCH — network-first for dynamic routes,
//         cache-first for static assets
// ─────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Static assets → cache-first
  const isStatic = url.pathname.startsWith('/static/');
  if (isStatic) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((resp) => {
          if (!resp || !resp.ok) return resp;
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
          return resp;
        });
      })
    );
    return;
  }

  // App routes → network-first, fall back to cache, then offline page
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
        }
        return resp;
      })
      .catch(() =>
        caches.match(event.request).then((cached) => cached || caches.match(OFFLINE_URL))
      )
  );
});

// ─────────────────────────────────────────────
// PUSH NOTIFICATIONS
// ─────────────────────────────────────────────
self.addEventListener('push', (event) => {
  let payload = { title: 'ConnectSafe', body: 'Diagnostic update available.', icon: '/static/icon-192.png' };
  try { if (event.data) payload = { ...payload, ...event.data.json() }; } catch (_) {}

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body:    payload.body,
      icon:    payload.icon || '/static/icon-192.png',
      badge:   '/static/icon-192.png',
      vibrate: [200, 100, 200],
      data:    { url: payload.url || '/client' },
      actions: [
        { action: 'open',    title: 'Open App' },
        { action: 'dismiss', title: 'Dismiss'  }
      ]
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'dismiss') return;

  const targetUrl = (event.notification.data && event.notification.data.url) || '/client';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(targetUrl);
    })
  );
});

// ─────────────────────────────────────────────
// BACKGROUND SYNC (keep-alive telemetry)
// ─────────────────────────────────────────────
self.addEventListener('sync', (event) => {
  if (event.tag === 'telemetry-sync') {
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then((clientList) => {
        clientList.forEach((c) => c.postMessage({ type: 'SW_SYNC_TELEMETRY' }));
      })
    );
  }
});
