# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import include, path, re_path

from apps.accounts.views import admin_login_redirect
from apps.common import csp, health, metrics
from apps.common.db_connections import releases_db_connections
from apps.common.views_dev import ui_kit
from apps.common.views_feedback import ProblemReportDoneView, ProblemReportView
from apps.session.api.v1.api import api as session_api_v1
from apps.session.views.invitation_responses import InvitationResponseView
from apps.work.faction.views.certificates import CertificateVerifyView
from apps.work.faction.views.feeds import PersonalCalendarFeedView
from insight_core.admin_monitoring import monitoring_view
from mandari import pwa
from mandari.media import serve_media


def health_check(request):
    """Health check endpoint for Docker/Kubernetes."""
    # Check database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"
    # Worker nur, wenn die Installation ihn braucht (apps.common.health.worker_state, Issue #509)
    worker_status = health.worker_state() if db_status == "ok" else "unbekannt"

    return JsonResponse(
        {
            "status": "ok" if db_status == "ok" and worker_status != "fehlt" else "degraded",
            "database": db_status,
            "worker": worker_status,
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
    # Offene Schnittstelle der Drehscheibe: Aggregator über den RIS-Bestand aller Kommunen (OParl 1.1)
    path("oparl/", include("hub.api.urls", namespace="oparl_api")),
    # Katalog der offenen Ratsinformationen nach DCAT-AP.de für Datenportale (Issue #104)
    path("data/dcat/", include("hub.adapters.dcat.urls", namespace="dcat")),
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
    # Rückmeldung zur Ladung (Issue #225) — signiertes Token je Ladungsempfänger, ohne Login
    path(
        "ladung/<str:token>/",
        InvitationResponseView.as_view(),
        name="session_invitation_response",
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
# Bewusst unabhängig von DEBUG registriert (siehe mandari/media.py). Vorhandene öffentliche
# Dateien liefert schon mandari.media.PublicMediaMiddleware aus, ohne Sitzung und Datenbank.
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
