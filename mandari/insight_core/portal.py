# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bürgerportal je Körperschaft (Issue #317, Teil A).

Jede gelistete Kommune hat einen eigenen Einstieg unter ``/insight/k/<slug>/`` – mit eigenem
Namen, Logo und optional einer Akzentfarbe. Die Kommunenauswahl ist dort auf diese Körperschaft
festgelegt; alle Links innerhalb des Portals bleiben in diesem Kontext, weil der Einstieg ihn in
der Sitzung des Browsers festhält (``PORTAL_SESSION_KEY``). „Alle Kommunen“ bzw. die Auswahl einer
anderen Kommune verlassen den Kontext wieder.

Optional ordnet ``settings.PORTAL_HOSTS`` einem eigenen Hostnamen fest eine Körperschaft zu. Die
Middleware wertet den Host-Header nur über ``request.get_host()`` aus (Prüfung gegen
``ALLOWED_HOSTS``) und nur für Hosts, die zusätzlich ausdrücklich in ``ALLOWED_HOSTS`` stehen – ein
beliebiger Host-Header wählt nie ein Portal. Auf einem solchen Host lässt sich der Kontext nicht
verlassen.

Der Slug ist der Slug der Kommune im Bürgerportal; für Session-Mandanten funktioniert auch ihr
Mandanten-Slug (die Kommune ihrer eigenen OParl-Quelle).

Sicherheit: Einstiege zeigen nur gelistete, nicht gelöschte Kommunen und damit dieselben
öffentlichen Daten wie das gemeinsame Portal. Weiterleitungen gehen nur auf geprüfte, relative
Pfade des Portals, nie auf Parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings
from django.core import checks
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound, HttpResponseRedirect
from django.http.request import split_domain_port, validate_host
from django.urls import Resolver404, resolve, reverse

if TYPE_CHECKING:
    from collections.abc import Callable

    from insight_core.models import OParlBody

#: Session-Schlüssel des Portal-Kontexts: {"body": <uuid>, "slug": <einstiegs-slug>}
PORTAL_SESSION_KEY = "insight_portal"
#: Branding aus dem Session-Mandanten (Logo, Primärfarbe) wird so lange zwischengespeichert
BRANDING_CACHE_SECONDS = 300
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
DEEP_LINK_RE = re.compile(r"[a-z0-9][a-z0-9/-]*")
#: Seiten, auf die ein Einstieg mit Pfad (``/insight/k/<slug>/termine/``) weiterleiten darf
DEEP_LINK_NAMES = frozenset(
    {
        "portal_home",
        "meeting_list",
        "meeting_calendar",
        "meeting_year_plan",
        "meeting_detail",
        "paper_list",
        "paper_detail",
        "organization_list",
        "organization_detail",
        "person_list",
        "person_detail",
        "decision_list",
        "decision_detail",
        "question_portal",
        "question_detail",
        "file_list",
        "search",
        "map",
        "neighborhood",
        "saved",
        "notifications",
        "chat",
    }
)
_UNSET = object()


@dataclass(frozen=True)
class PortalContext:
    """Aktiver Portal-Kontext einer Anfrage (Template-Variable ``insight_portal``)."""

    body: OParlBody
    via_host: bool
    slug: str
    name: str
    logo_url: str | None
    accent_color: str | None

    @property
    def home_url(self) -> str:
        if self.via_host:
            return reverse("insight_core:insight:portal_home")
        return reverse("insight_core:insight:portal_entry", kwargs={"slug": self.slug})

    @property
    def can_leave(self) -> bool:
        """Den Kontext verlassen (zur gemeinsamen Auswahl) geht nur beim Einstieg über den Pfad."""
        return not self.via_host


# ---------------------------------------------------------------------------
# Auflösung
# ---------------------------------------------------------------------------


def resolve_body(slug: str) -> OParlBody | None:
    """
    Gelistete Kommune zum Einstiegs-Slug: Slug der Kommune, sonst Mandanten-Slug in Session.

    Die Zuordnung Slug → Kommune wird kurz zwischengespeichert; ob die Kommune gelistet ist, prüft
    jeder Aufruf neu (eine Abfrage).
    """
    from insight_core.models import OParlBody

    schluessel = f"insight_portal_slug:{slug}"
    bekannt = cache.get(schluessel)
    if bekannt:
        body = cast("OParlBody | None", OParlBody.objects.listed().filter(pk=bekannt).first())
        if body is not None:
            return body
    body = _find_body(slug)
    if body is not None:
        cache.set(schluessel, str(body.pk), BRANDING_CACHE_SECONDS)
    return body


def _find_body(slug: str) -> OParlBody | None:
    from insight_core.models import OParlBody

    body = cast("OParlBody | None", OParlBody.objects.listed().filter(slug=slug).first())
    if body is not None:
        return body
    from apps.session.models import SessionTenant
    from apps.session.services.insight_service import session_sources

    tenant = SessionTenant.objects.filter(slug=slug, is_active=True, insight_publish=True).first()
    if tenant is None:
        return None
    body = OParlBody.objects.listed().filter(source__in=session_sources(tenant)).order_by("created_at").first()
    if body is None and tenant.oparl_body_id:
        body = OParlBody.objects.listed().filter(pk=tenant.oparl_body_id).first()
    return cast("OParlBody | None", body)


def host_slug(request: HttpRequest) -> str | None:
    """Portal-Slug zum Host der Anfrage – nur für ausdrücklich zugelassene Hosts (ALLOWED_HOSTS)."""
    hosts: dict[str, str] = getattr(settings, "PORTAL_HOSTS", {}) or {}
    if not hosts:
        return None
    domain, _port = split_domain_port(request.get_host())  # get_host() prüft gegen ALLOWED_HOSTS
    slug = hosts.get(domain)
    if not slug or not validate_host(domain, settings.ALLOWED_HOSTS):
        return None
    return slug


def get_portal(request: HttpRequest) -> PortalContext | None:
    """Portal-Kontext der Anfrage (einmal je Anfrage ermittelt); ``None`` im gemeinsamen Portal."""
    cached = getattr(request, "_insight_portal", _UNSET)
    if cached is not _UNSET:
        return cast("PortalContext | None", cached)
    portal = _resolve(request)
    request._insight_portal = portal  # type: ignore[attr-defined]
    return portal


def _resolve(request: HttpRequest) -> PortalContext | None:
    from insight_core.models import OParlBody

    slug = getattr(request, "insight_portal_host_slug", None)
    if slug:
        body = getattr(request, "insight_portal_host_body", None) or resolve_body(slug)
        return build_context(body, via_host=True, slug=slug) if body is not None else None
    session = getattr(request, "session", None)
    state = session.get(PORTAL_SESSION_KEY) if session is not None else None
    if not isinstance(state, dict) or not state.get("body"):
        return None
    try:
        body = OParlBody.objects.listed().filter(pk=state["body"]).first()
    except (ValidationError, ValueError):
        body = None
    if body is None:
        # Kommune nicht mehr gelistet (z. B. Mandant deaktiviert): zurück ins gemeinsame Portal
        leave(request)
        return None
    return build_context(body, via_host=False, slug=str(state.get("slug") or body.slug or ""))


def build_context(body: OParlBody, *, via_host: bool, slug: str) -> PortalContext:
    fallback = tenant_branding(body)
    logo_url = body.logo.url if body.logo else fallback.get("logo_url")
    accent = body.accent_color if body.accent_color and COLOR_RE.fullmatch(body.accent_color) else None
    return PortalContext(
        body=body,
        via_host=via_host,
        slug=slug or str(body.slug or ""),
        name=str(cast(Any, body).get_display_name()),
        logo_url=logo_url,
        accent_color=accent or fallback.get("accent_color"),
    )


def tenant_branding(body: OParlBody) -> dict[str, str | None]:
    """Logo und Primärfarbe des Session-Mandanten hinter der Kommune (zwischengespeichert)."""

    def laden() -> dict[str, str | None]:
        tenant = _tenant_for_body(body)
        if tenant is None:
            return {"logo_url": None, "accent_color": None}
        farbe = tenant.primary_color if COLOR_RE.fullmatch(tenant.primary_color or "") else None
        return {"logo_url": tenant.logo.url if tenant.logo else None, "accent_color": farbe}

    wert = cache.get_or_set(f"insight_portal_branding:{body.pk}", laden, BRANDING_CACHE_SECONDS)
    return cast("dict[str, str | None]", wert or {})


def _tenant_for_body(body: OParlBody) -> Any:
    from apps.session.models import SessionTenant

    tenant = SessionTenant.objects.filter(oparl_body=body, is_active=True).first()
    if tenant is not None:
        return tenant
    config = body.source.sync_config if isinstance(body.source.sync_config, dict) else {}
    slug = config.get("session_tenant")
    if not slug:
        return None
    return SessionTenant.objects.filter(slug=slug, is_active=True).first()


# ---------------------------------------------------------------------------
# Kontext betreten und verlassen, Weiterleitung
# ---------------------------------------------------------------------------


def enter(request: HttpRequest, body: OParlBody, slug: str) -> PortalContext:
    """Einstieg über den Pfad: Kontext in der Sitzung festhalten und die Kommune wählen."""
    zustand = {"body": str(body.pk), "slug": slug}
    # Nur bei Änderung schreiben: Ein wiederholter Aufruf des Einstiegs speichert die Sitzung nicht neu
    if request.session.get(PORTAL_SESSION_KEY) != zustand:
        request.session[PORTAL_SESSION_KEY] = zustand
    if request.session.get("active_body_id") != str(body.pk):
        request.session["active_body_id"] = str(body.pk)
    portal = build_context(body, via_host=False, slug=slug)
    request._insight_portal = portal  # type: ignore[attr-defined]
    return portal


def leave(request: HttpRequest) -> None:
    """Pfad-Kontext verlassen (gemeinsames Portal); auf einem Portal-Host wirkungslos."""
    session = getattr(request, "session", None)
    if session is not None and PORTAL_SESSION_KEY in session:
        del session[PORTAL_SESSION_KEY]
    request._insight_portal = None  # type: ignore[attr-defined]


def deep_link_target(rest: str) -> str | None:
    """
    Relativer Portalpfad für ``/insight/k/<slug>/<rest>`` – nur, wenn er auf eine erlaubte Seite
    des Portals führt. Nie ein Host, nie ein Parameter: Keine offene Weiterleitung.
    """
    if not DEEP_LINK_RE.fullmatch(rest or "") or "//" in rest:
        return None
    path = f"/insight/{rest}"
    try:
        match = resolve(path)
    except Resolver404:
        return None
    if match.namespace != "insight_core:insight" or match.url_name not in DEEP_LINK_NAMES:
        return None
    # Ziel aus der aufgelösten Route neu bauen statt den Eingabepfad weiterzureichen
    return reverse(f"{match.namespace}:{match.url_name}", args=match.args, kwargs=match.kwargs)


# ---------------------------------------------------------------------------
# Middleware und Systemprüfung
# ---------------------------------------------------------------------------


class PortalHostMiddleware:
    """
    Eigener Hostname je Körperschaft (``settings.PORTAL_HOSTS``).

    Ohne Einträge tut die Middleware nichts (kein Zugriff auf den Host-Header). Sonst markiert sie
    Anfragen eines zugeordneten, in ``ALLOWED_HOSTS`` stehenden Hosts, leitet ``/`` auf das Portal
    und antwortet für Portalseiten mit 404, solange die Kommune nicht gelistet ist – ein Portal-Host
    fällt nie auf das gemeinsame Portal zurück.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        slug = host_slug(request)
        request.insight_portal_host_slug = slug  # type: ignore[attr-defined]
        if slug is not None:
            if request.path_info in ("", "/"):
                return HttpResponseRedirect(reverse("insight_core:insight:portal_home"))
            if request.path_info.startswith("/insight/"):
                body = resolve_body(slug)
                if body is None:
                    return HttpResponseNotFound("Dieses Bürgerportal ist derzeit nicht verfügbar.")
                request.insight_portal_host_body = body  # type: ignore[attr-defined]
        return self.get_response(request)


@checks.register(checks.Tags.security)
def check_portal_hosts(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """Jeder Host in PORTAL_HOSTS muss auch in ALLOWED_HOSTS stehen, sonst bleibt er wirkungslos."""
    meldungen: list[checks.CheckMessage] = []
    for host in getattr(settings, "PORTAL_HOSTS", {}) or {}:
        if not validate_host(host, settings.ALLOWED_HOSTS):
            meldungen.append(
                checks.Warning(
                    f"PORTAL_HOSTS enthält „{host}“, der nicht in ALLOWED_HOSTS steht; der Eintrag wirkt nicht.",
                    id="insight_core.W001",
                )
            )
    return meldungen
