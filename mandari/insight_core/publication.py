# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Veröffentlichungsstand einer Kommune im Bürgerportal (Issue #618).

Beendet ein Herausgeber – bisher ein Session-Mandant – die Veröffentlichung, bleibt der gespiegelte
Bestand in der Datenbank. Wie das Bürgerportal ihn zeigt, bestimmt der Stand seiner Quelle
(``OParlSource.sync_config["portal_state"]``; additiv wie ``source_type``, der Ingestor kennt ihn
nicht und braucht keine neue Spalte). Der Stand gilt für alle Kommunen der Quelle:

``paused`` – vorübergehend abgeschaltet
    Jede Seite der Kommune antwortet mit 503, ``Retry-After`` und einem Hinweis statt Inhalt. Die
    kommunenübergreifende Suche und die Merkliste blenden ihre Einträge aus; Sitemap und
    OParl-Schnittstelle antworten ebenfalls mit 503. Nichts wird gelöscht, Wiedereinschalten stellt
    alles her.
``archived`` – als Archiv behalten
    Der Bestand bleibt lesbar (Seiten, Suche, Sitemap, OParl), jede Seite der Kommune trägt deutlich
    „nicht mehr aktuell“. Aktualisiert wird nichts mehr.
``withdrawn`` – dauerhaft zurückgenommen
    Die Einträge selbst sind vom Herausgeber zurückgenommen (``mark_deleted``: 410 auf den
    Detailseiten, Tombstones in OParl, raus aus Suche und Sitemaps); die Kommune ist nicht mehr
    gelistet. Ihre übrigen Seiten, ihr Einstieg und ihre Sitemap antworten mit 410.

Die Stände aller Kommunen liegen als kleine Tabelle im Cache (Redis in Produktion, fünf Minuten);
Setzen leert ihn. Solange keine Kommune einen Stand hat, kostet die Prüfung keine Abfrage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

PAUSED = "paused"
ARCHIVED = "archived"
WITHDRAWN = "withdrawn"
MODES = frozenset({PAUSED, ARCHIVED, WITHDRAWN})

#: Schlüssel in ``OParlSource.sync_config``: {"mode": …, "since": ISO-Zeitpunkt}
STATE_KEY = "portal_state"
CACHE_KEY = "insight_publication_states:v1"
CACHE_SECONDS = 300
#: Empfohlene Wartezeit bis zum nächsten Versuch bei vorübergehender Abschaltung (Sekunden)
RETRY_AFTER_SECONDS = 3600


@dataclass(frozen=True)
class BodyState:
    """Veröffentlichungsstand einer Kommune (Template-Variable ``publication_state``)."""

    mode: str
    since: datetime | None = None

    @property
    def paused(self) -> bool:
        return self.mode == PAUSED

    @property
    def archived(self) -> bool:
        return self.mode == ARCHIVED

    @property
    def withdrawn(self) -> bool:
        return self.mode == WITHDRAWN


# ---------------------------------------------------------------------------
# Stand lesen und setzen
# ---------------------------------------------------------------------------


def _parse(record: Any) -> BodyState | None:
    if not isinstance(record, dict) or record.get("mode") not in MODES:
        return None
    since = None
    if isinstance(record.get("since"), str):
        try:
            since = datetime.fromisoformat(record["since"])
        except ValueError:
            since = None
    return BodyState(mode=str(record["mode"]), since=since)


def source_state(source: Any) -> BodyState | None:
    """Stand einer Quelle (``None``: Veröffentlichung läuft bzw. nie beendet)."""
    config = source.sync_config if isinstance(source.sync_config, dict) else {}
    return _parse(config.get(STATE_KEY))


def set_source_state(source: Any, mode: str | None, *, since: datetime | None = None) -> bool:
    """
    Stand an einer Quelle setzen, ``None`` hebt ihn auf; ``True`` bei Änderung.

    Ein gleicher Stand bleibt mit seinem ursprünglichen Zeitpunkt stehen. Gespeichert wird nur
    ``sync_config`` – die Aktivität der Quelle (Sync an/aus) steuert der Herausgeber.
    """
    if mode is not None and mode not in MODES:
        raise ValueError(f"Unbekannter Veröffentlichungsstand: {mode}")
    config = dict(source.sync_config) if isinstance(source.sync_config, dict) else {}
    current = _parse(config.get(STATE_KEY))
    if mode is None:
        if STATE_KEY not in config:
            return False
        config.pop(STATE_KEY)
    else:
        if current is not None and current.mode == mode:
            return False
        config[STATE_KEY] = {"mode": mode, "since": (since or timezone.now()).isoformat()}
    source.sync_config = config
    source.save(update_fields=["sync_config", "updated_at"])
    invalidate()
    return True


def invalidate() -> None:
    cache.delete(CACHE_KEY)


def states() -> dict[str, BodyState]:
    """Stand je Kommune (Schlüssel: Body-ID als Text); leer, solange keine Quelle einen Stand hat."""
    cached = cache.get(CACHE_KEY)
    if isinstance(cached, dict):
        return {body_id: BodyState(mode, _since(since)) for body_id, (mode, since) in cached.items()}
    from .models import OParlBody, OParlSource

    table: dict[str, tuple[str, str | None]] = {}
    for source in OParlSource.objects.filter(sync_config__has_key=STATE_KEY).only("id", "sync_config"):
        state = source_state(source)
        if state is None:
            continue
        since = state.since.isoformat() if state.since else None
        for body_id in OParlBody.objects.filter(source=source).values_list("id", flat=True):
            table[str(body_id)] = (state.mode, since)
    # Nur einfache Werte in den Cache: Ein älteres Image liest den Schlüssel nie, ein neueres kann
    # die Form ändern, ohne an alten Pickles zu scheitern
    cache.set(CACHE_KEY, table, CACHE_SECONDS)
    return {body_id: BodyState(mode, _since(since)) for body_id, (mode, since) in table.items()}


def _since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def body_state(body_id: Any) -> BodyState | None:
    if body_id is None:
        return None
    table = states()
    return table.get(str(body_id)) if table else None


def paused_body_ids() -> set[str]:
    """Kommunen, deren Einträge Suche, Merkliste und Listen derzeit ausblenden."""
    return {body_id for body_id, state in states().items() if state.paused}


# ---------------------------------------------------------------------------
# Kommune eines Eintrags
# ---------------------------------------------------------------------------


def _body_paths() -> dict[type, tuple[str, ...]]:
    from .models import (
        OParlAgendaItem,
        OParlBody,
        OParlConsultation,
        OParlFile,
        OParlLegislativeTerm,
        OParlLocation,
        OParlMeeting,
        OParlMembership,
        OParlOrganization,
        OParlPaper,
        OParlPerson,
        PublicQuestion,
    )

    return {
        OParlBody: ("id",),
        OParlOrganization: ("body",),
        OParlPerson: ("body",),
        OParlMeeting: ("body",),
        OParlPaper: ("body",),
        OParlAgendaItem: ("meeting__body",),
        OParlFile: ("body", "paper__body", "meeting__body"),
        OParlMembership: ("organization__body", "person__body"),
        OParlLocation: ("body",),
        OParlConsultation: ("body", "paper__body"),
        OParlLegislativeTerm: ("body",),
        PublicQuestion: ("body",),
    }


def body_id_of(model: type, pk: Any) -> str | None:
    """Kommune eines Eintrags per Primärschlüssel (eine Abfrage); ``None``, wenn unbekannt."""
    paths = _body_paths().get(model)
    if not paths:
        return None
    try:
        row = model.objects.filter(pk=pk).values_list(*paths).first()  # type: ignore[attr-defined]
    except (ValueError, TypeError):
        return None
    if not row:
        return None
    return next((str(value) for value in row if value), None)


# ---------------------------------------------------------------------------
# Antworten
# ---------------------------------------------------------------------------


def _page(request: HttpRequest, state: BodyState, status: int) -> HttpResponse:
    html = render_to_string("pages/publication_state.html", {"publication_state": state}, request=request)
    response = HttpResponse(html, status=status)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    if status == 503:
        response["Retry-After"] = str(RETRY_AFTER_SECONDS)
    if request.headers.get("HX-Request") == "true":
        # HTMX tauscht Fehlerantworten nicht ein; neu laden zeigt den Hinweis
        response["HX-Refresh"] = "true"
    return response


def state_response(request: HttpRequest, state: BodyState) -> HttpResponse | None:
    """Hinweisseite statt Inhalt: 503 bei vorübergehender Abschaltung, 410 bei Rücknahme."""
    if state.paused:
        return _page(request, state, 503)
    if state.withdrawn:
        return _page(request, state, 410)
    return None


# ---------------------------------------------------------------------------
# Middleware für die Seiten des Bürgerportals
# ---------------------------------------------------------------------------

#: Detailseiten: URL-Name → (Modellname, URL-Parameter mit dem Primärschlüssel)
DETAIL_PAGES: dict[str, tuple[str, str]] = {
    "meeting_detail": ("OParlMeeting", "pk"),
    "paper_detail": ("OParlPaper", "pk"),
    "paper_summary": ("OParlPaper", "pk"),
    "organization_detail": ("OParlOrganization", "pk"),
    "person_detail": ("OParlPerson", "pk"),
    "ask_question": ("OParlPerson", "pk"),
    "file_proxy": ("OParlFile", "file_id"),
    "question_detail": ("PublicQuestion", "pk"),
}

#: Seiten der gewählten Kommune (Liste, Kalender, Karte, Suche …) und ihre HTMX-Teile
BODY_PAGES = frozenset(
    {
        "portal_home",
        "organization_list",
        "person_list",
        "decision_list",
        "question_portal",
        "question_start",
        "paper_list",
        "meeting_list",
        "meeting_calendar",
        "calendar_feed",
        "meeting_year_plan",
        "calendar_events",
        "file_list",
        "search",
        "search_results",
        "map",
        "map_markers",
        "neighborhood",
        "neighborhood_autocomplete",
        "neighborhood_results",
        "chat",
        "chat_message",
    }
)
PORTAL_NAMESPACE = "insight_core:insight"


def _active_body_id(request: HttpRequest) -> str | None:
    """Gewählte Kommune wie ``get_active_body`` – ohne dessen Rückfall auf die erste Kommune."""
    from .portal import get_portal

    portal = get_portal(request)
    if portal is not None:
        return str(portal.body.pk)
    session = getattr(request, "session", None)
    body_id = session.get("active_body_id") if session is not None else None
    if not body_id or body_id == "all":
        return None
    return str(body_id)


def _page_body_id(request: HttpRequest, url_name: str, kwargs: dict[str, Any]) -> str | None:
    detail = DETAIL_PAGES.get(url_name)
    if detail is not None:
        from django.apps import apps

        model_name, key = detail
        return body_id_of(apps.get_model("insight_core", model_name), kwargs.get(key))
    if url_name in BODY_PAGES:
        return _active_body_id(request)
    return None


class PublicationStateMiddleware:
    """
    Setzt den Veröffentlichungsstand auf den Seiten des Bürgerportals durch.

    Vorübergehend abgeschaltet: 503 mit Hinweis; zurückgenommen: 410; Archiv: Die Seite läuft
    normal, ``request.insight_publication_state`` trägt den Stand für den Hinweis im Layout.
    Maßgeblich ist bei Detailseiten die Kommune des Eintrags, sonst die gewählte Kommune.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: dict[str, Any]
    ) -> HttpResponse | None:
        match = getattr(request, "resolver_match", None)
        if match is None or match.namespace != PORTAL_NAMESPACE:
            return None
        if not states():
            return None
        state = body_state(_page_body_id(request, match.url_name or "", view_kwargs))
        if state is None:
            return None
        request.insight_publication_state = state  # type: ignore[attr-defined]
        return state_response(request, state)
