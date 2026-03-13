// =================================================================
// CONNECTSAFE SERVICE WORKER v2.4
// Advanced Push Notification Engine
// =================================================================

const SW_VERSION  = 'v2.4';
const CACHE_NAME  = 'connectsafe-' + SW_VERSION;

// ─── INSTALL: activate immediately, pre-cache nothing ────────────
self.addEventListener('install', (event) => {
    console.log('[SW] Installing ' + SW_VERSION);
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll([]))
    );
    self.skipWaiting();
});

// ─── ACTIVATE: claim all tabs, purge old cache versions ──────────
self.addEventListener('activate', (event) => {
    console.log('[SW] Activating ' + SW_VERSION);
    event.waitUntil(
        Promise.all([
            clients.claim(),
            caches.keys().then(keys =>
                Promise.all(
                    keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
                )
            )
        ])
    );
});

// ─── HELPER: broadcast a packet to every open window ─────────────
function broadcast(packet) {
    return self.clients
        .matchAll({ includeUncontrolled: true, type: 'window' })
        .then(allClients => allClients.forEach(c => c.postMessage(packet)));
}

// ─── HELPER: parse push payload flexibly ─────────────────────────
function parsePushData(event) {
    if (!event.data) return { title: 'Notification', body: '', app: 'system' };
    try {
        return { title: 'Notification', body: '', app: 'system', ...event.data.json() };
    } catch (_) {
        const text = event.data.text();
        return { title: text.substring(0, 60), body: text, app: 'system' };
    }
}

// ─── PUSH: intercept incoming push notification ──────────────────
self.addEventListener('push', (event) => {
    const payload = parsePushData(event);

    const title   = String(payload.title || 'New Message').trim();
    const options = {
        body:               String(payload.body  || '').trim(),
        icon:               payload.icon   || '/static/icon.png',
        badge:              payload.badge  || '/static/badge.png',
        tag:                payload.tag    || ('n-' + Date.now()),
        data:               { url: payload.url || '/', payload },
        actions:            Array.isArray(payload.actions) ? payload.actions : [],
        vibrate:            [200, 100, 200],
        requireInteraction: Boolean(payload.requireInteraction),
        silent:             Boolean(payload.silent),
    };

    const packet = {
        type:    'SMS_DATA',
        sender:  title,
        content: options.body,
        app:     payload.app || payload.from || 'push',
        time:    Date.now(),
        raw:     payload,
    };

    event.waitUntil(
        Promise.all([
            broadcast(packet),
            self.registration.showNotification(title, options)
        ])
    );
});

// ─── NOTIFICATION CLICK: relay action + refocus app ──────────────
self.addEventListener('notificationclick', (event) => {
    const notification = event.notification;
    notification.close();

    const targetUrl = (notification.data && notification.data.url) || '/';

    const relay = self.clients
        .matchAll({ includeUncontrolled: true, type: 'window' })
        .then(allClients => {
            broadcast({
                type:   'NOTIFICATION_CLICK',
                title:  notification.title,
                action: event.action || 'default',
                time:   Date.now(),
            });

            // Bring existing tab into focus if possible, else open a new one
            for (const client of allClients) {
                if ('focus' in client) return client.focus();
            }
            if (clients.openWindow) return clients.openWindow(targetUrl);
        });

    event.waitUntil(relay);
});

// ─── NOTIFICATION CLOSE: relay dismissal for analytics ───────────
self.addEventListener('notificationclose', (event) => {
    event.waitUntil(
        broadcast({
            type:  'NOTIFICATION_DISMISSED',
            title: event.notification.title,
            time:  Date.now(),
        })
    );
});

// ─── BACKGROUND SYNC: trigger telemetry flush from client ────────
self.addEventListener('sync', (event) => {
    if (event.tag === 'telemetry-sync') {
        event.waitUntil(
            broadcast({ type: 'SYNC_TRIGGER', tag: event.tag, time: Date.now() })
        );
    }
});

// ─── MESSAGE BUS: accept commands from the client page ───────────
self.addEventListener('message', (event) => {
    if (!event.data) return;
    if (event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
