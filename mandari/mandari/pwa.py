# SPDX-License-Identifier: AGPL-3.0-or-later
"""PWA-Endpunkte: Web App Manifest, Service Worker und Offline-Fallback.

Alle drei Views sind bewusst unauthentifiziert und frei von Nutzerdaten:
Das Manifest beschreibt nur die App-Hülle, der Service Worker cached
ausschließlich statische Assets und die Offline-Seite (niemals HTML mit
authentifizierten Inhalten — Cache Storage ist unverschlüsselt).
"""

import hashlib

from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET


def _cache_version() -> str:
    """Build-abhängige Cache-Version für den Service Worker.

    Abgeleitet aus den (bei ``CompressedManifestStaticFilesStorage``
    inhaltsgehashten) URLs der Kern-Assets: Ein neues Image mit geänderten
    Assets ergibt automatisch eine neue Version — alte Caches werden beim
    ``activate`` aufgeräumt. Im DEBUG-Betrieb (ungehashte URLs) bleibt die
    Version konstant ("dev"-Verhalten, unkritisch).
    """
    seed = "|".join(
        static(path)
        for path in (
            "css/styles.css",
            "vendor/alpine/alpine.min.js",
            "vendor/htmx/htmx.min.js",
            "vendor/lucide/lucide.min.js",
        )
    )
    return hashlib.md5(seed.encode()).hexdigest()[:12]


@require_GET
def manifest(request):
    """Web App Manifest — macht mandari work am Startbildschirm installierbar."""
    data = {
        "name": "mandari",
        "short_name": "mandari",
        "description": "mandari work — Arbeitsbereich für Fraktionen und kommunalpolitische Organisationen.",
        "lang": "de",
        "start_url": "/work/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": "#4F46E5",
        "background_color": "#faf9f7",
        "icons": [
            {"src": static("brand/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("brand/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {
                "src": static("brand/icon-maskable-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": static("brand/icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    response = JsonResponse(data, content_type="application/manifest+json")
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def service_worker(request):
    """Service Worker unter /sw.js — Root-Scope für /work/ und /accounts/."""
    response = render(
        request,
        "pwa/sw.js",
        {"cache_version": _cache_version()},
        content_type="text/javascript; charset=utf-8",
    )
    # Immer frisch prüfen, damit neue Versionen zügig aktiv werden.
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def offline(request):
    """Offline-Fallback-Seite (wird vom Service Worker vorgecacht)."""
    response = render(request, "pages/offline.html")
    response["Cache-Control"] = "no-cache"
    return response
