# SPDX-License-Identifier: AGPL-3.0-or-later
"""
URL configuration for Mandari project.

Mandari Insight - Kommunalpolitische Transparenz
"""

from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import URLPattern, URLResolver, include, path, re_path

from apps.accounts.views import admin_login_redirect
from apps.common import csp, health, metrics, module
from apps.common.db_connections import releases_db_connections
from apps.common.module import nur_mit
from apps.common.views_dev import ui_kit
from apps.common.views_feedback import ProblemReportDoneView, ProblemReportView
from insight_ai.katalog import AI_CATALOG_TYPE, katalog_view
from insight_ai.mcp import endpoint as mcp_endpoint
from insight_ai.mcp import server_card_view as mcp_server_card
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


# =============================================================================
# Adressen der Module (Issue #1594): nur eingeschaltete Module, ihre Ansichten erst dann importiert
# =============================================================================


def work_schnittstelle() -> list[URLPattern | URLResolver]:
    """Öffentliche Fraktions-API v1 (Issue #71): read-only, Opt-in je Organisation, opakes Token."""
    # Subdomain api.mandari.de wäre reines Caddy-Routing, gleiche Pfade
    return [path("api/public/v1/", include("apps.work.faction.public_api", namespace="faction_public_api"))]


def session_schnittstelle() -> list[URLPattern | URLResolver]:
    """Session-API v1 (django-ninja, OpenAPI unter /api/v1/session/openapi.json, Issue #163)."""
    from apps.session.api.v1.api import api as session_api_v1

    return [path("api/v1/session/", session_api_v1.urls)]


def session_nichtoeffentlich() -> list[URLPattern | URLResolver]:
    """
    Metadaten der nichtöffentlichen Ausgabe als geschützte Ressource (RFC 9728, Issue #565); ohne
    OPARL_NONPUBLIC_ENABLED 404. Die Adresse bildet RFC 9728 aus der Adresse der Ausgabe.
    """
    from apps.session.api import nichtoeffentlich as session_noe

    return [
        path(
            ".well-known/oauth-protected-resource/session/<slug:tenant_slug>/api/oparl/nichtoeffentlich/",
            session_noe.metadata_view,
            name="oparl_noe_metadata",
        ),
    ]


def session_seiten() -> list[URLPattern | URLResolver]:
    """Session RIS (Verwaltung) und die Rückmeldung zur Ladung."""
    from apps.session.views.invitation_responses import InvitationResponseView

    return [
        path("session/", include("apps.session.urls", namespace="session")),
        # Rückmeldung zur Ladung (Issue #225) — signiertes Token je Ladungsempfänger, ohne Login
        path("ladung/<str:token>/", InvitationResponseView.as_view(), name="session_invitation_response"),
    ]


def work_seiten() -> list[URLPattern | URLResolver]:
    """Work (Arbeitsbereich der Organisationen), Teilnahmenachweise und persönlicher Kalender."""
    from apps.work.faction.views.certificates import CertificateVerifyView
    from apps.work.faction.views.feeds import PersonalCalendarFeedView

    return [
        path("work/", include("apps.work.urls", namespace="work")),
        # Öffentliche Verifikation von Teilnahmenachweisen (Issue #68) — opakes Token, ohne Login, ohne Personenbezug
        path("nachweis/<slug:token>/", CertificateVerifyView.as_view(), name="certificate_verify"),
        # Persönlicher iCal-Feed (Issue #70) — opakes Token, ohne Login (Kalender-Clients können sich nicht anmelden)
        path("kalender/feed/<slug:token>.ics", PersonalCalendarFeedView.as_view(), name="personal_calendar_feed"),
    ]


def mcp_adressen() -> list[URLPattern | URLResolver]:
    """
    Öffentlicher, nur lesender MCP-Server des Bürgerportals für KI-Werkzeuge (Issue #899); Standard aus
    (INSIGHT_MCP_ENABLED). Unter /insight/, damit der Reverse-Proxy ihn ohne eigene Regel an die Anwendung gibt.
    """
    return [
        path("insight/mcp", mcp_endpoint, name="insight_mcp"),
        path("insight/mcp/", mcp_endpoint),
        # Server Card des MCP-Servers (SEP-2127, Issue #1309)
        path("insight/mcp/server-card", mcp_server_card, name="insight_mcp_server_card"),
    ]


urlpatterns = [
    # Health check (for Docker/Kubernetes)
    path("health/", health_check, name="health_check"),
    # Getrennte Liveness-/Readiness-Prüfungen (Issue #231)
    path("health/live/", health.live, name="health_live"),
    path("health/ready/", health.ready, name="health_ready"),
    # Worker für die Statusseite: Lebenszeichen, Rückstau, Fehlerquote (Issue #574)
    path("health/worker/", health.worker, name="health_worker"),
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
    *nur_mit(module.WORK, work_schnittstelle),
    *nur_mit(module.SESSION, session_schnittstelle),
    # Public API (stats, contact form - consumed by Wagtail marketing site)
    # Befehle der gemeinsamen Schnittstelle (Issue #1588): POST /api/mandari/v1/<bereich>/<mandant>/befehle/<befehl>/v<n>
    path("api/mandari/v1/", include("hub.api.befehle", namespace="befehle")),
    path("api/", include("insight_core.api_urls")),
    # Provisioning-API fürs Billing-Portal (nur aktiv wenn PROVISIONING_API_KEY gesetzt)
    path("api/provisioning/", include("apps.provisioning.urls", namespace="provisioning")),
    # Provisioning-API v2, deklarativ (nur aktiv wenn PROVISIONING_V2_API_KEY gesetzt, Issue #1132)
    path("api/provisioning/v2/", include("apps.provisioning.v2.urls", namespace="provisioning_v2")),
    # Einstieg der Schnittstelle (Issue #1205): /api/, /api/openapi.json (eine Beschreibung aller Bereiche) und
    # /api/docs/. Nach den Bereichen unter /api/, die genau drei Adressen fangen keine von deren Pfaden ab.
    path("api/", include("hub.api.entry_urls", namespace="schnittstelle")),
    # Offene Schnittstelle der Drehscheibe: Aggregator über den RIS-Bestand aller Kommunen (OParl 1.1)
    path("oparl/", include("hub.api.urls", namespace="oparl_api")),
    # Katalog der offenen Ratsinformationen nach DCAT-AP.de für Datenportale (Issue #104)
    path("data/dcat/", include("hub.adapters.dcat.urls", namespace="dcat")),
    # Authentication (login, logout, password reset)
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    # OpenID-Connect-Anbieter (Issue #1206): /oauth/… und /.well-known/openid-configuration; ohne
    # OIDC_PROVIDER_ENABLED antworten alle mit 404
    path("", include("apps.accounts.oidc.urls")),
    *nur_mit(module.SESSION, session_nichtoeffentlich),
    *nur_mit(module.SESSION, session_seiten),
    *nur_mit(module.WORK, work_seiten),
    *nur_mit(module.PORTAL, mcp_adressen),
    # Katalog für KI-Agenten (ARD, AI Catalog; Issue #1309)
    path(".well-known/ard.json", katalog_view, name="ki_katalog_ard"),
    path(".well-known/ai-catalog.json", katalog_view, {"medientyp": AI_CATALOG_TYPE}, name="ki_katalog"),
    # Insight Core (RIS Portal, public protocols, body sitemaps); ohne Bürgerportal nur die gemeinsamen Adressen
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
#
# Gerendert werden die Fehlerseiten ohne Kontextprozessoren (Issue #1024): Die Vorlagen
# (errors/base_error.html) brauchen nur ``request`` und ``csp_nonce``. Die Kontextprozessoren
# fragten dagegen bei jeder 404 die Datenbank ab und legten für Besucher ohne Sitzung eine an
# (Kommunenauswahl „alle“), also eine Zeile in django_session je Scanner-Anfrage. Dasselbe traf
# jede Weiterleitung von APPEND_SLASH: CommonMiddleware rendert erst die 404 und ersetzt sie
# danach durch die Weiterleitung.


def fehlerseite(
    request: HttpRequest, template_name: str, status: int, kontext: dict[str, object] | None = None
) -> HttpResponse:
    """Fehlerseite ohne Kontextprozessoren, also ohne Datenbankzugriff und ohne Sitzung."""
    from django.template.context_processors import csp
    from django.template.loader import render_to_string

    inhalt = render_to_string(template_name, {"request": request, **csp(request), **(kontext or {})})
    return HttpResponse(inhalt, status=status)


#: Fehlerseiten mit eigener Fassung im Stil des Bürgerportals (Issue #1261): Status → Vorlage
INSIGHT_FEHLERSEITEN = {
    404: "errors/insight_404.html",
    410: "errors/insight_410.html",
    500: "errors/insight_500.html",
}

#: Detailseiten des Bürgerportals, deren Kennung sich nie ändert (``uuid5`` der Adresse in der Quelle, ADR
#: ``docs/adr/20260929-kanonisches-modell.md``). Ihre Ansicht meldet „nicht gefunden“ nur, wenn es das Objekt
#: nicht mehr gibt; vorübergehend abgeschaltete Kommunen antworten vorher mit 503, zurückgenommene Einträge mit 410.
ENTFERNTE_DETAILSEITEN = frozenset({"paper_detail", "meeting_detail", "person_detail", "organization_detail"})


def entfernte_detailseite(request: HttpRequest) -> bool:
    """Gilt die 404 einer Detailseite des Bürgerportals, deren Objekt nicht mehr existiert?

    Dann antwortet die Seite mit ``410 Gone`` statt 404 (Search Console „Nicht gefunden (404)“, 18.11.2026: rund
    18.000 Adressen von Einträgen, die Kommunen entfernt oder neu angelegt haben). Suchmaschinen geben eine 410
    schneller auf und fragen sie seltener erneut ab. Die Adresse ist schon aufgelöst (``resolver_match``), also
    ohne Datenbankzugriff.
    """
    treffer = getattr(request, "resolver_match", None)
    return (
        treffer is not None
        and treffer.url_name in ENTFERNTE_DETAILSEITEN
        and treffer.namespace.split(":")[-1] == "insight"
    )


def im_buergerportal(request: HttpRequest) -> bool:
    """Gehört die Anfrage zum Bürgerportal? Pfad unter /insight/ oder eigener Host einer Körperschaft.

    Ohne Datenbank: Den Host ordnet die PortalHostMiddleware vorher zu (``insight_portal_host_slug``); fiel der
    Fehler davor, zählt nur der Pfad. Ohne Bürgerportal (Issue #1594) gibt es dessen Fehlerseiten nicht.
    """
    if not module.aktiv(module.PORTAL):
        return False
    return request.path_info.startswith("/insight/") or bool(getattr(request, "insight_portal_host_slug", None))


def fehlerseite_fuer(
    request: HttpRequest, template_name: str, status: int, kontext: dict[str, object] | None = None
) -> HttpResponse:
    """Fehlerseite des Bereichs: im Bürgerportal dessen Fassung (Sie-Form, Suche), sonst die gemeinsame Vorlage.

    Work, Session und Konto behalten ``template_name`` (errors/base_error.html); die Website hat eigene Seiten.
    """
    insight = INSIGHT_FEHLERSEITEN.get(status)
    if insight is not None and im_buergerportal(request):
        marketing = str(getattr(settings, "MARKETING_URL", "") or "")
        return fehlerseite(request, insight, status, {"marketing_url": marketing, **(kontext or {})})
    return fehlerseite(request, template_name, status, kontext)


@releases_db_connections
def handler_400(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Bad Request error handler."""
    return fehlerseite(request, "400.html", 400)


@releases_db_connections
def handler_403(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Permission Denied error handler."""
    return fehlerseite(request, "403.html", 403)


@releases_db_connections
def handler_404(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """Page Not Found error handler; entfernte Detailseiten des Bürgerportals mit 410 (``entfernte_detailseite``)."""
    if entfernte_detailseite(request):
        return fehlerseite_fuer(request, "404.html", 410)
    return fehlerseite_fuer(request, "404.html", 404)


@releases_db_connections
def handler_500(request: HttpRequest) -> HttpResponse:
    """Server Error handler.

    Fehlt die Datenbank (erschöpfter Pool, Ausfall), gibt es eine 503-Seite ohne
    Datenbankzugriff — auch dann, wenn der Fehler nicht aus der View, sondern aus einer
    Middleware kam (Issue #344). Die Fehlerseite selbst braucht keine Datenbank (``fehlerseite``);
    scheitert sie trotzdem (etwa an einem ungültigen Host für den Link „Problem melden“), wird
    sie ohne ``request`` gerendert.
    """
    import sys
    import uuid

    from django.template.loader import render_to_string

    from apps.common.db_connections import is_pool_exhausted
    from apps.common.middleware import database_unavailable_response, is_database_unavailable

    ausnahme = sys.exc_info()[1]
    if is_database_unavailable(ausnahme):
        return database_unavailable_response(request, pool=is_pool_exhausted(ausnahme))

    kontext: dict[str, object] = {"request_id": str(uuid.uuid4())[:8]}
    try:
        return fehlerseite_fuer(request, "500.html", 500, kontext)
    except Exception:  # noqa: BLE001 - die Fehlerseite darf nicht selbst zum zweiten Fehler werden
        return HttpResponse(render_to_string("500.html", kontext), status=500)


# Register custom error handlers
handler400 = handler_400
handler403 = handler_403
handler404 = handler_404
handler500 = handler_500
