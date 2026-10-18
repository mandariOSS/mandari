# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recherche-Suche in Work im neuen Erscheinungsbild (Issue #853): die Bausteine der Insight-Suche mit dem Bezug der
Fraktion.

* **Treffer nach Vorgang gruppiert** mit Kontextzeile (Art, Aktenzeichen, Gremium, Datum), Stand-Satz aus dem
  Beratungsverlauf und sauberem Ausschnitt (``insight_core.services.search_presentation``); Links führen auf die
  RIS-Seiten in Work.
* **Filterleiste wie im Bürgerportal** (Reiter, Zeitraum, Art, Sortierung) und dazu, was Work schon hatte: Gremium,
  Kommune (bei mehreren) und ein eigener Zeitraum. Alte Links (``von``, ``bis``, ``gremium``, ``art``, ``typ``,
  ``seite``, ``kommune``) gelten weiter (``insight_core.services.search_filters.SearchQuery``).
* **Gewichtung** nach Aktualität (Abfrage v2) und nach dem Bezug der Fraktion (``bezug``): eigene Anträge, bearbeitete
  Vorgänge, vorbereitete Sitzungen, eigene Gremien. Nur Faktoren, keine Filter; ohne eigene Daten dieselbe Reihenfolge
  wie im Bürgerportal.
* Ohne Suchdienst sucht die Seite in Titeln und Aktenzeichen der Datenbank (wie bisher) und sagt das. Wie bisher
  führt je Art „Alle anzeigen“ auf die vollständige Liste (Vorgänge, Sitzungen, Gremien, Personen) mit dem Suchbegriff.

Kontext und Ergebnisbereich sind dieselben wie im Bürgerportal: ``insight_core.services.search_page.build_context``
und ``partials/search_page.html``. Hier steht nur, was Work dazu hat (Bezug, Gremien- und Kommunenauswahl,
Datenbanksuche).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any, Final
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape

from insight_core.services.search_filters import PERIOD_CUSTOM, SearchQuery
from insight_core.services.search_page import active_filters, build_context
from insight_core.services.search_presentation import Links, german_date, normalize_paper_type, statuses_for_papers

from . import selectors
from .bezug import Fraktionsbezug, fraktionsbezug
from .selectors import Bodies
from .services import resolve_search_bodies

logger = logging.getLogger(__name__)

#: Art des Treffers → Adressname in Work und Name der Kennung
WORK_ADRESSEN: Final = {
    "vorgang": ("work:ris_paper_detail", "paper_id"),
    "sitzung": ("work:ris_meeting_detail", "meeting_id"),
    "person": ("work:ris_person_detail", "person_id"),
    "gremium": ("work:ris_organization_detail", "org_id"),
}
#: Datenbanksuche ohne Suchdienst: je Art die vollständige Liste in Work (wie „Alle anzeigen“ der bisherigen Seite)
WORK_LISTEN: Final = (
    ("papers", "work:ris_papers", "Vorgänge"),
    ("meetings", "work:ris_meetings", "Sitzungen"),
    ("organizations", "work:ris_organizations", "Gremien"),
    ("persons", "work:ris_persons", "Personen"),
)


def work_links(org_slug: str) -> Links:
    """Adressen der Treffer auf den RIS-Seiten in Work statt im Bürgerportal."""

    def link(kind: str, pk: Any) -> str:
        name, kennung = WORK_ADRESSEN[kind]
        try:
            return reverse(name, kwargs={"org_slug": org_slug, kennung: pk})
        except NoReverseMatch:
            return reverse("work:ris_search", kwargs={"org_slug": org_slug})

    return link


def zu_kurz(params: SearchQuery) -> bool:
    """Nichts zu suchen: ohne Begriff und Filter, oder nur ein Zeichen (findet zu viel und nichts Sinnvolles)."""
    return params.is_empty or (len(params.query) < 2 and not params.has_filters)


def ganze_seite(headers: Mapping[str, str]) -> bool:
    """Ganze Seite statt Ausschnitt: kein HTMX oder Wiederherstellung aus dem Verlauf."""
    return headers.get("HX-Request") != "true" or headers.get("HX-History-Restore-Request") == "true"


def teilvorlage(headers: Mapping[str, str], context: Mapping[str, Any]) -> str | None:
    """Antwort auf einen Austausch per HTMX: Vorlage des Ausschnitts, ``""`` für einen leeren Ergebnisbereich (Feld
    geleert oder zu kurz), ``None`` für die ganze Seite (kein HTMX oder Wiederherstellung aus dem Verlauf)."""
    if ganze_seite(headers):
        return None
    if context.get("suche_leer") or context.get("no_body_linked"):
        return ""
    if int(context.get("page") or 1) > 1:
        return "partials/search_results_liste.html"
    return "partials/search_page.html"


def ergebnis(
    organization: Any,
    membership: Any,
    params: SearchQuery,
    bodies: Bodies,
    *,
    service: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Alles für Seite und Austausch per HTMX: Treffer, Zahl, Reiter, Filter; ohne Suchdienst die Datenbanksuche."""
    body_ids, body_filter = resolve_search_bodies(bodies, params.body_filter)
    params = replace(params, body_filter=body_filter)
    bezug = fraktionsbezug(organization, membership, bodies)
    links = work_links(organization.slug)
    heute = today or timezone.localdate()
    kontext: dict[str, Any] = {"params": params, "query": params.query, "page": params.page, "suche_work": True}
    kontext.update(_sachfilter(params, bodies))
    try:
        if service is None:
            from insight_core.services.search_service import get_search_service

            service = get_search_service()
        kontext.update(
            build_context(service, params, None, body_ids, heute, links=links, boost=bezug.boost(), custom_period=True)
        )
        for treffer in kontext["groups"]:
            treffer["bezug"] = bezug.text(treffer["kind"], treffer["pk"], treffer["gremien"])
    except Exception as exc:  # Suchdienst nicht erreichbar: Datenbanksuche, die Seite bleibt nutzbar
        logger.warning("Work-Suche ohne Elasticsearch, Datenbanksuche: %s", type(exc).__name__)
        kontext.update(_datenbank(params, body_ids, bezug, links, heute, organization.slug))
    kontext["active_filters"] = active_filters(params) + [
        {"label": f"Kommune: {option['label']}", "url": params.url(body_filter="")}
        for option in kontext["kommune_options"]
        if option["checked"]
    ]
    return kontext


def _sachfilter(params: SearchQuery, bodies: Bodies) -> dict[str, Any]:
    """Filter, die Work zusätzlich hat: Gremium (Name) und Kommune (bei mehreren Kommunen)."""
    namen = list(dict.fromkeys(name for name in selectors.search_committees(bodies) if name))
    if params.committee and params.committee not in namen:
        namen.insert(0, params.committee)  # alter Link mit einem Gremium, das keine Sitzung mehr hat
    kommunen = list(bodies)
    return {
        "zeitraum_frei": {"von": params.date_from, "bis": params.date_to} if params.period == PERIOD_CUSTOM else "",
        "gremium_options": [{"value": name, "label": name, "checked": name == params.committee} for name in namen],
        "kommune_options": [
            {"value": str(b.pk), "label": b.get_display_name(), "checked": str(b.pk) == params.body_filter}
            for b in kommunen
        ]
        if len(kommunen) > 1
        else [],
    }


def _datenbank(
    params: SearchQuery, body_ids: list[str], bezug: Fraktionsbezug, links: Links, today: date, org_slug: str
) -> dict[str, Any]:
    """Ohne Suchdienst: Titel und Aktenzeichen aus der Datenbank, im selben Trefferformat (ohne Ausschnitt).

    ``selectors.orm_search`` liefert je Art höchstens zehn Einträge; ``alle_anzeigen`` führt für jede Art mit Treffern
    auf die vollständige Liste in Work mit dem Suchbegriff (``q``), wie die bisherige Seite.
    """
    date_from, date_to = params.date_range(today)
    funde = selectors.orm_search(
        body_ids,
        query=params.query,
        date_from=date_from or "",
        date_to=date_to or "",
        paper_type=params.paper_type,
        committee=params.committee,
    )
    staende = statuses_for_papers([str(p.pk) for p in funde["papers"]])
    vorgaenge = []
    for paper in funde["papers"]:
        status = staende.get(str(paper.pk))
        vorgaenge.append(
            _treffer(
                "vorgang",
                paper.pk,
                links,
                paper.name or paper.reference or "Vorgang",
                [normalize_paper_type(paper.paper_type), paper.reference or "", german_date(paper.date)],
                bezug,
                status=getattr(status, "text", "") or "Noch keine Beratung bekannt.",
                status_kind=getattr(status, "kind", ""),
            )
        )
    sitzungen = [
        _treffer(
            "sitzung",
            meeting.pk,
            links,
            meeting.get_display_name(),
            ["Sitzung", ", ".join(o.name for o in meeting.organizations.all() if o.name), german_date(meeting.start)],
            bezug,
        )
        for meeting in funde["meetings"]
    ]
    gremien = [
        _treffer(
            "gremium", o.pk, links, o.short_name or o.name or "Gremium", ["Gremium", o.organization_type or ""], bezug
        )
        for o in funde["organizations"]
    ]
    personen = [_treffer("person", p.pk, links, p.name or "Person", ["Person"], bezug) for p in funde["persons"]]
    nach_bezug = sorted(vorgaenge, key=lambda t: -t["faktor"]) + sorted(sitzungen, key=lambda t: -t["faktor"])
    reiter = {
        "": nach_bezug,
        "papers": vorgaenge,
        "meetings": sitzungen,
        "files": [],
        "persons": personen,
        "organizations": gremien,
    }
    groups = reiter.get(params.result_type, nach_bezug)
    if not params.result_type:
        groups = nach_bezug + gremien + personen
    n = len(groups)
    suchbegriff = urlencode({"q": params.query}) if params.query else ""
    alle_anzeigen = [
        {
            "label": label,
            "url": reverse(name, kwargs={"org_slug": org_slug}) + (f"?{suchbegriff}" if suchbegriff else ""),
        }
        for art, name, label in WORK_LISTEN
        if funde[art]
    ]
    return {
        "groups": groups,
        "datenbanksuche": True,
        "alle_anzeigen": alle_anzeigen,
        "count_sentence": f"{n} Treffer in Titeln und Aktenzeichen"
        if n != 1
        else "1 Treffer in Titeln und Aktenzeichen",
        "has_more": False,
        "tabs": [],
        "art_options": [],
        "period_options": [],
        "sort_options": [],
    }


def _treffer(
    kind: str,
    pk: Any,
    links: Links,
    titel: str,
    kontext: list[str],
    bezug: Fraktionsbezug,
    *,
    status: str = "",
    status_kind: str = "",
) -> dict[str, Any]:
    kennung = str(pk)
    return {
        "kind": kind,
        "pk": kennung,
        "gremien": [],
        "url": links(kind, pk),
        "title": escape(titel),
        "context": [teil for teil in kontext if teil],
        "status": status,
        "status_kind": status_kind,
        "snippet": None,
        "fundstelle": None,
        "others": [],
        "bezug": bezug.text(kind, kennung),
        "faktor": bezug.faktor(kind, kennung),
    }
