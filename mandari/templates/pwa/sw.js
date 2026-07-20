{% load static %}// SPDX-License-Identifier: AGPL-3.0-or-later
// Mandari Service Worker — bewusst konservativ:
//   * HTML/Navigationen: IMMER network-first und NIE in den Cache schreiben
//     (Cache Storage ist unverschlüsselt — keine authentifizierten Inhalte ablegen).
//   * /static/: cache-first (Dateinamen sind inhaltsgehasht, also immutable).
//   * Offline: vorgecachte Fallback-Seite nur für Navigationsanfragen.
// Versionierung: CACHE_VERSION kommt aus dem Django-View (Build/Release-Kennung);
// neue Version übernimmt beim nächsten Laden (skipWaiting + clients.claim).

const CACHE_VERSION = '{{ cache_version|escapejs }}';
const STATIC_CACHE = 'mandari-static-' + CACHE_VERSION;
const OFFLINE_CACHE = 'mandari-offline-' + CACHE_VERSION;
const OFFLINE_URL = '{% url "pwa_offline" %}';

// Kern-Assets fürs App-Shell-Gefühl (gehashte Namen => sicher cachebar).
const PRECACHE_STATIC = [
    '{% static "css/styles.css" %}',
    '{% static "vendor/alpine/alpine.min.js" %}',
    '{% static "vendor/htmx/htmx.min.js" %}',
    '{% static "vendor/lucide/lucide.min.js" %}',
    '{% static "brand/icon-192.png" %}',
    '{% static "brand/favicon.svg" %}',
];

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const offlineCache = await caches.open(OFFLINE_CACHE);
        await offlineCache.add(new Request(OFFLINE_URL, { cache: 'reload' }));
        const staticCache = await caches.open(STATIC_CACHE);
        // Einzeln addieren: ein fehlendes Asset darf die Installation nicht kippen.
        await Promise.allSettled(PRECACHE_STATIC.map((url) => staticCache.add(url)));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(
            keys
                .filter((key) => key.startsWith('mandari-') && key !== STATIC_CACHE && key !== OFFLINE_CACHE)
                .map((key) => caches.delete(key))
        );
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    // Navigationen (HTML): network-first, KEIN Caching der Antwort.
    if (request.mode === 'navigate') {
        event.respondWith((async () => {
            try {
                return await fetch(request);
            } catch (err) {
                const cached = await caches.match(OFFLINE_URL, { cacheName: OFFLINE_CACHE });
                return cached || Response.error();
            }
        })());
        return;
    }

    // Statische Assets: cache-first (inhaltsgehashte Dateinamen).
    if (url.pathname.startsWith('/static/')) {
        event.respondWith((async () => {
            const cached = await caches.match(request, { cacheName: STATIC_CACHE });
            if (cached) return cached;
            const response = await fetch(request);
            if (response.ok) {
                const cache = await caches.open(STATIC_CACHE);
                cache.put(request, response.clone());
            }
            return response;
        })());
        return;
    }

    // Alles andere (API, Media, HTMX-Partials …): nicht anfassen — direkt Netz.
});
