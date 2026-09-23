# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import include, path, re_path
from django.views.static import serve as static_serve

from apps.accounts.views import admin_login_redirect
from apps.common import csp, health, metrics
from apps.common.db_connections import releases_db_connections
from apps.common.uploads import is_embeddable
from apps.common.views_dev import ui_kit
from apps.common.views_feedback import ProblemReportDoneView, ProblemReportView
from apps.session.api.v1.api import api as session_api_v1
from apps.work.faction.views.certificates import CertificateVerifyView
from apps.work.faction.views.feeds import PersonalCalendarFeedView
from insight_core.admin_monitoring import monitoring_view
from mandari import pwa

#: Medien, die ohne Anmeldung ausgeliefert werden (Logos, Hero-Bilder, Demo).
PUBLIC_MEDIA_PREFIXES = (
    "bodies/",
    "persons/photos/",
    "organizations/logos/",
    "parties/logos/",
    "session/tenants/logos/",
    "avatars/",
    "demo/",
)

#: Medien, die NIE direkt ausgeliefert werden – nur über zugriffsgeprüfte
#: Download-Views (Session-Anlagen, Dokument-Anhänge im Work-Portal).
PROTECTED_MEDIA_PREFIXES = (
    "session/files/",
    "motions/documents/",
)


def serve_media(request, path):
    """Serve uploaded media files (logos, uploads) via Django.

    In Produktion proxied Caddy /media/* an Django. Der frühere
    ``static()``-Helper ist bei DEBUG=False ein No-Op und lieferte
    dort für alle Uploads 404. ``django.views.static.serve`` kümmert
    sich um Last-Modified/304; wir ergänzen einen moderaten Cache-Header.

    Sicherheit (drei Stufen):
    - PROTECTED_MEDIA_PREFIXES werden hier NIE ausgeliefert – sie können
      nichtöffentlich sein und sind nur über die zugriffsgeprüften
      Download-Views erreichbar (Session-Anlagen, Dokument-Anhänge).
    - PUBLIC_MEDIA_PREFIXES (Logos, Hero-Bilder) sind ohne Anmeldung
      abrufbar.
    - Alle übrigen Uploads (z. B. Anhänge von Aufgaben, Fraktionssitzungen,
      Support) erfordern mindestens eine Anmeldung; sie sind nicht mehr
      per bloßer URL-Kenntnis für Dritte abrufbar.
    """
    from django.http import Http404

    if path.startswith(PROTECTED_MEDIA_PREFIXES):
        raise Http404("Diese Datei wird nur über die geschützte Download-View ausgeliefert.")
    if not path.startswith(PUBLIC_MEDIA_PREFIXES) and not request.user.is_authenticated:
        raise Http404("Datei nicht gefunden.")
    response = static_serve(request, path, document_root=settings.MEDIA_ROOT)
    response["Cache-Control"] = (
        "public, max-age=3600" if path.startswith(PUBLIC_MEDIA_PREFIXES) else "private, no-store"
    )
    # Zweite Verteidigungslinie zur Upload-Pruefung (Issue #260): Nur Bildformate
    # werden eingebettet ausgeliefert. Alles andere geht als Download hinaus, damit
    # eine Datei nicht im Ursprung der Anwendung zur Anzeige und Ausfuehrung kommt.
    if not is_embeddable(path):
        dateiname = PurePosixPath(path).name.replace('"', "")
        response["Content-Disposition"] = f'attachment; filename="{dateiname}"'
    response["X-Content-Type-Options"] = "nosniff"
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
    # Getrennte Liveness-/Readiness-Prüfungen (Issue #231)
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
    # Prometheus-Metriken; nur intern (METRICS_ALLOWED_NETWORKS / METRICS_TOKEN), sonst 404
    path("metrics/", metrics.metrics_view, name="metrics"),
    path("csp-report/", csp.csp_report, name="csp_report"),
    # PWA: Manifest, Service Worker (Root-Scope), Offline-Fallback
    path("manifest.webmanifest", pwa.manifest, name="pwa_manifest"),
    path("sw.js", pwa.service_worker, name="pwa_sw"),
    path("offline/", pwa.offline, name="pwa_offline"),
    # „Problem melden": Fehlermeldung -> Ticket im Admin-Dashboard
    path("feedback/", ProblemReportView.as_view(), name="problem_report"),
    path("feedback/<str:reference>/danke/", ProblemReportDoneView.as_view(), name="problem_report_done"),
    # Admin custom endpoints (must come before admin.site.urls)
    path("admin/insight_sync/trigger-sync/", include("insight_sync.admin_urls")),
    path("admin/monitoring/", monitoring_view, name="admin_monitoring"),
    # Redirect admin logout to custom logout (Django 5+ admin only accepts POST)
    path("admin/logout/", lambda request: redirect("accounts:logout")),
    # Admin-Anmeldung nur über die eigene Anmeldung (Ratenbegrenzung, zweiter Faktor)
    path("admin/login/", admin_login_redirect, name="admin_login_redirect"),
    # Admin
    path("admin/", admin.site.urls),
    # Öffentliche Fraktions-API v1 (Issue #71): read-only, Opt-in je
    # Organisation, opakes Token — pfadbasiert unter /api/public/v1/
    # (Subdomain api.mandari.de wäre reines Caddy-Routing, gleiche Pfade)
    path("api/public/v1/", include("apps.work.faction.public_api", namespace="faction_public_api")),
    # Public API (stats, contact form - consumed by Wagtail marketing site)
    # Session-API v1 (django-ninja, OpenAPI unter /api/v1/session/openapi.json, Issue #163)
    path("api/v1/session/", session_api_v1.urls),
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
    # Öffentliche Verifikation von Teilnahmenachweisen (Issue #68) —
    # opakes Token, ohne Login, ohne Personenbezug
    path(
        "nachweis/<slug:token>/",
        CertificateVerifyView.as_view(),
        name="certificate_verify",
    ),
    # Persönlicher iCal-Feed (Issue #70) — opakes Token, ohne Login
    # (Kalender-Clients können sich nicht anmelden)
    path(
        "kalender/feed/<slug:token>.ics",
        PersonalCalendarFeedView.as_view(),
        name="personal_calendar_feed",
    ),
    # Insight Core (RIS Portal, public protocols, body sitemaps)
    path("", include("insight_core.urls")),
]

# Komponentenvorschau (UI-Kit) – nur in der Entwicklung, siehe apps/common/views_dev.py
if settings.DEBUG or getattr(settings, "UI_KIT_PREVIEW", False):
    urlpatterns += [path("dev/ui/", ui_kit, name="dev_ui_kit")]

# Serve media files (logos, uploads) — in production via Caddy → Django.
# Bewusst unabhängig von DEBUG registriert (siehe serve_media-Docstring).
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve_media, name="media"),
]


# =============================================================================
# Custom Error Handlers
# =============================================================================


# Alle Fehler-Handler geben am Ende die Datenbankverbindungen ihres Threads zurück (#344).
# Unter ASGI ruft Django sie über response_for_exception mit thread_sensitive=False auf,
# also in den langlebigen Threads des Standard-Executors. Die schließen nie eine Anfrage ab:
# Jeder, der einmal eine Fehlerseite mit Datenbankzugriff (Anmeldestatus, Navigation)
# gerendert hat, behielte seine Verbindung für immer. Der Executor hat zehn Threads, der
# Pool zehn Verbindungen — eine 404-Welle eines Scanners genügte, um ihn ganz zu belegen.


@releases_db_connections
def handler_400(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Bad Request error handler."""
    return render(request, "400.html", status=400)


@releases_db_connections
def handler_403(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Permission Denied error handler."""
    return render(request, "403.html", status=403)


@releases_db_connections
def handler_404(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Page Not Found error handler."""
    return render(request, "404.html", status=404)


@releases_db_connections
def handler_500(request: HttpRequest) -> HttpResponse:
    """Server Error handler.

    Fehlt die Datenbank (erschöpfter Pool, Ausfall), gibt es eine 503-Seite ohne
    Datenbankzugriff — auch dann, wenn der Fehler nicht aus der View, sondern aus einer
    Middleware kam (Issue #344). Scheitert die normale Fehlerseite selbst, weil ihre
    Kontextprozessoren die Datenbank brauchen, wird sie ohne Request-Kontext gerendert.
    """
    import sys
    import uuid

    from django.template.loader import render_to_string

    from apps.common.db_connections import is_pool_exhausted
    from apps.common.middleware import database_unavailable_response, is_database_unavailable

    ausnahme = sys.exc_info()[1]
    if is_database_unavailable(ausnahme):
        return database_unavailable_response(request, pool=is_pool_exhausted(ausnahme))

    kontext = {"request_id": str(uuid.uuid4())[:8]}
    try:
        return render(request, "500.html", kontext, status=500)
    except Exception:  # noqa: BLE001 - die Fehlerseite darf nicht selbst zum zweiten Fehler werden
        return HttpResponse(render_to_string("500.html", kontext), status=500)


# Register custom error handlers
handler400 = handler_400
handler403 = handler_403
handler404 = handler_404
handler500 = handler_500
