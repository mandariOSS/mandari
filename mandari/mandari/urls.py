# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import include, path, re_path
from django.views.static import serve as static_serve

from mandari import pwa


def serve_media(request, path):
    """Serve uploaded media files (logos, uploads) via Django.

    In Produktion proxied Caddy /media/* an Django. Der frühere
    ``static()``-Helper ist bei DEBUG=False ein No-Op und lieferte
    dort für alle Uploads 404. ``django.views.static.serve`` kümmert
    sich um Last-Modified/304; wir ergänzen einen moderaten Cache-Header.
    """
    response = static_serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Cache-Control"] = "public, max-age=3600"
    return response


def health_check(request):
    """Health check endpoint for Docker/Kubernetes."""
    # Check database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"

    return JsonResponse(
        {
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
        }
    )


urlpatterns = [
    # Health check (for Docker/Kubernetes)
    path("health/", health_check, name="health_check"),
    # PWA: Manifest, Service Worker (Root-Scope), Offline-Fallback
    path("manifest.webmanifest", pwa.manifest, name="pwa_manifest"),
    path("sw.js", pwa.service_worker, name="pwa_sw"),
    path("offline/", pwa.offline, name="pwa_offline"),
    # Admin custom endpoints (must come before admin.site.urls)
    path("admin/insight_sync/trigger-sync/", include("insight_sync.admin_urls")),
    # Redirect admin logout to custom logout (Django 5+ admin only accepts POST)
    path("admin/logout/", lambda request: redirect("accounts:logout")),
    # Admin
    path("admin/", admin.site.urls),
    # Public API (stats, contact form - consumed by Wagtail marketing site)
    path("api/", include("insight_core.api_urls")),
    # Provisioning-API fürs Billing-Portal (nur aktiv wenn PROVISIONING_API_KEY gesetzt)
    path("api/provisioning/", include("apps.provisioning.urls", namespace="provisioning")),
    # OParl-1.1-Aggregations-API: mandari als eigene OParl-Datenquelle (Issue #17)
    path("oparl/", include("oparl_api.urls", namespace="oparl_api")),
    # Authentication (login, logout, password reset)
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    # Session RIS (administrative portal)
    path("session/", include("apps.session.urls", namespace="session")),
    # Work module (portal for organizations)
    path("work/", include("apps.work.urls", namespace="work")),
    # Insight Core (RIS Portal, public protocols, body sitemaps)
    path("", include("insight_core.urls")),
]

# Serve media files (logos, uploads) — in production via Caddy → Django.
# Bewusst unabhängig von DEBUG registriert (siehe serve_media-Docstring).
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve_media, name="media"),
]


# =============================================================================
# Custom Error Handlers
# =============================================================================


def handler_400(request, exception=None):
    """Bad Request error handler."""
    return render(request, "400.html", status=400)


def handler_403(request, exception=None):
    """Permission Denied error handler."""
    return render(request, "403.html", status=403)


def handler_404(request, exception=None):
    """Page Not Found error handler."""
    return render(request, "404.html", status=404)


def handler_500(request):
    """Server Error handler."""
    import uuid

    return render(request, "500.html", {"request_id": str(uuid.uuid4())[:8]}, status=500)


# Register custom error handlers
handler400 = handler_400
handler403 = handler_403
handler404 = handler_404
handler500 = handler_500
