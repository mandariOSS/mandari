# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchseite (Konzept Insight-Suche, P0.8/P0.9): Parameter, Reiter, Filter, Ortsband – für Bürgerportal und Work.

Es gibt **einen** Kontextaufbau (``build_context``) und **eine** Parameterklasse (``search_filters.SearchQuery``) für
beide Suchseiten (Issue #853). Was Work zusätzlich hat, steckt in den Parametern (Gremium, Art als Originalwert,
Kommune, eigener Zeitraum) und in den Angaben ``links`` (Adressen der Treffer in Work), ``boost`` (Bezug der Fraktion)
und ``custom_period`` (Option „Eigener Zeitraum“); ohne sie fragt die Seite den Suchdienst wie das Bürgerportal.
Alte Links mit ``type=paper`` gelten weiter. Die Seite fragt Elasticsearch einmal für Liste, Zahl und alle Reiter
(``search_grouped`` mit Gewichten je Reiter) und einmal für die Zähler der Filter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from django.utils.text import slugify

from .search_filters import PERIOD_CUSTOM, PERIODS, SORTS, SearchQuery
from .search_presentation import Links, count_sentence, german_date, normalize_paper_type, present_groups

if TYPE_CHECKING:
    from insight_core.models import OParlBody

#: Reiter: Parameterwert, Beschriftung, Gewichte der Rangfusion, Arten der Gruppen (``None`` = alle)
TABS: Final[tuple[tuple[str, str, dict[str, float], set[str] | None], ...]] = (
    ("", "Alle", {"papers": 1.0, "files": 0.9, "meetings": 0.6}, None),
    ("papers", "Vorgänge", {"papers": 1.0, "files": 0.9}, {"paper"}),
    ("meetings", "Sitzungen", {"meetings": 1.0, "files": 0.9}, {"meeting"}),
    ("files", "Dokumente", {"files": 1.0}, None),
    ("persons", "Personen", {"persons": 1.0}, {"person"}),
    ("organizations", "Gremien", {"organizations": 1.0}, {"organization"}),
)
#: Reiter, auf die Gremium und Art als Originalwert nicht wirken (Personen, Gremien)
_WITHOUT_DOCUMENT_FILTERS: Final = ("persons", "organizations")
PAGE_SIZE: Final = 20

#: Früherer Name der Parameter des Bürgerportals; seit Issue #853 dieselbe Klasse wie in Work
SearchParams = SearchQuery


def papers_of_types(
    labels: list[str], body_slug: str, raw: tuple[str, ...] | list[str] = ()
) -> Callable[[set[str]], set[str]]:
    """Filter für Dateien: Vorgänge, deren normalisierte Art gewählt ist (eine Datenbankabfrage je Suche).

    ``raw`` schränkt zusätzlich auf Originalwerte ein (Filter ``art`` in Work, Issue #853); leer heißt ohne.
    """
    gewaehlt = set(labels)
    originale = set(raw)

    def passt(art: str | None) -> bool:
        if labels and normalize_paper_type(art, body_slug) not in gewaehlt:
            return False
        return not originale or (art or "") in originale

    def erlaubt(paper_ids: set[str]) -> set[str]:
        import uuid

        from insight_core.models import OParlPaper

        kennungen = []
        for pid in paper_ids:
            try:
                kennungen.append(uuid.UUID(pid))
            except ValueError:
                continue
        rows = OParlPaper.objects.filter(pk__in=kennungen).values_list("pk", "paper_type")
        return {str(pk) for pk, art in rows if passt(art)}

    return erlaubt


def _raw_types(params: SearchQuery, raw_by_label: Mapping[str, list[str]]) -> list[str] | None:
    """Arten als Originalwerte für den Suchdienst: zusammengefasste Arten und der Filter ``art`` aus Work zugleich."""
    raws: list[str] | None = None
    if params.paper_types:
        raws = [raw for label in params.paper_types for raw in raw_by_label.get(label, [])] or ["∅"]
    if params.paper_type:
        raws = [params.paper_type] if raws is None else ([r for r in raws if r == params.paper_type] or ["∅"])
    return raws


def build_context(
    service: Any,
    params: SearchQuery,
    body: OParlBody | None,
    body_ids: list[str] | None,
    today: date,
    *,
    links: Links | None = None,
    boost: Any = None,
    custom_period: bool = False,
) -> dict[str, Any]:
    """
    Alles, was Seite und Austausch per HTMX brauchen: Liste, Zahl, Reiter, Filter, Ortsband.

    Args:
        body: gewählte Kommune (Bürgerportal) oder ``None`` für eine Suche über ``body_ids``
        links: Adressen der Treffer (Standard: Seiten des Bürgerportals; Work übergibt seine RIS-Seiten)
        boost: Gewichtung nach Bezug (``search_ranking.RelationBoost``, Work); nur Faktoren, keine Filter
        custom_period: Option „Eigener Zeitraum“ (``von``/``bis``) im Zeitraum-Filter anbieten (Work)
    """
    from .search_places import detect_place, place_band

    slug = (body.slug or "") if body is not None else ""
    body_id = str(body.pk) if body is not None else None
    date_from, date_to = params.date_range(today)
    # Bereich der Suche (gilt auch für „ohne Filter“) und Filter, die nur Work kennt; ohne Work-Angaben dieselben
    # Aufrufe wie im Bürgerportal
    scope: dict[str, Any] = {"body_id": body_id, "body_ids": body_ids if body is None else None}
    if boost is not None:
        scope["boost"] = boost
    filters: dict[str, Any] = {"organization_name": params.committee} if params.committee else {}

    facets = service.facet_counts(params.query, date_from=date_from, date_to=date_to, **scope, **filters)
    art_counts: dict[str, int] = {}
    raw_by_label: dict[str, list[str]] = {}
    for raw, n in facets["paper_types"].items():
        label = normalize_paper_type(raw, slug) or raw
        art_counts[label] = art_counts.get(label, 0) + n
        raw_by_label.setdefault(label, []).append(raw)

    arten = sorted(art_counts.items(), key=lambda item: -item[1])[:12]

    tab = next(t for t in TABS if t[0] == params.result_type)
    kinds = tab[3]
    by_type = bool(params.paper_types or params.paper_type)
    if by_type:
        # „Art“ gibt es nur für Vorgänge: Sitzungen und Unterlagen ohne Vorgang fallen mit dem Filter heraus
        kinds = {"paper"} if kinds is None else kinds & {"paper"}
    if (params.committee or params.paper_type) and params.result_type not in _WITHOUT_DOCUMENT_FILTERS:
        # Gremium und Art als Originalwert wirken nur auf Vorgänge, Sitzungen und Dokumente
        filters["index_names"] = ["papers", "meetings", "files"]
    grouped = service.search_grouped(
        params.query,
        page=params.page,
        page_size=PAGE_SIZE,
        date_from=date_from,
        date_to=date_to,
        paper_type=_raw_types(params, raw_by_label),
        sort=params.sort,
        weights=tab[2],
        kinds=kinds,
        file_paper_filter=papers_of_types(
            params.paper_types, slug, raw=[params.paper_type] if params.paper_type else []
        )
        if by_type
        else None,
        **scope,
        **filters,
    )
    counts: Mapping[str, Any] = grouped["counts"]
    totals: Mapping[str, int] = grouped["totals_by_index"]
    groups = present_groups(grouped["groups"], slug, links=links)

    tab_counts = {
        "": int(counts.get("vorgaenge", 0)) + int(counts.get("unterlagen", 0)) + int(counts.get("meetings", 0)),
        "papers": int(counts.get("vorgaenge", 0)),
        "meetings": int(totals.get("meetings", 0)),
        "files": int(totals.get("files", 0)),
        "persons": int(totals.get("persons", 0)),
        "organizations": int(totals.get("organizations", 0)),
    }
    tabs = [
        {
            "key": key,
            "label": label,
            "count": format_count(tab_counts[key]),
            "url": params.url(result_type=key),
            "active": key == params.result_type,
        }
        for key, label, _weights, _kinds in TABS
        if key == "" or key == params.result_type or tab_counts[key]
    ]

    band = None
    place = None
    if params.page == 1 and params.ort and body is not None and params.query and not params.has_filters:
        place = detect_place(body, params.query)
        if place is not None:
            band = place_band(body, place, params.query)

    context: dict[str, Any] = {
        "params": params,
        "query": params.query,
        "groups": groups,
        "count_sentence": count_sentence(counts),
        "similar_spelling": grouped["similar_spelling"],
        "has_more": grouped["has_more"],
        "page": params.page,
        "next_url": params.url(page=params.page + 1),
        "tabs": tabs,
        "art_options": [
            {
                "value": label,
                "kennung": kennung,
                "count": format_count(n),
                "checked": label in params.paper_types,
                "url": _art_url(params, label),
            }
            for (label, n), kennung in zip(arten, option_ids([label for label, _n in arten]), strict=True)
        ],
        "period_options": period_options(params, facets["periods"], custom=custom_period),
        "sort_options": [{"value": v, "label": label, "checked": v == params.sort} for v, label in SORTS],
        "active_filters": active_filters(params),
        "place_band": band,
        "place_name": place.name if place is not None else "",
        "word_only_url": params.url(ort=False) if band else "",
        "place_url": params.url(ort=True) if not params.ort else "",
    }
    if not groups and replace(params, body_filter="").has_filters:
        # „Ohne Filter“: ohne Filter des Inhalts, im selben Bereich (gewählte Kommune bleibt)
        ohne = service.search_grouped(params.query, page=1, page_size=1, weights=tab[2], kinds=tab[3], **scope)
        context["without_filters_count"] = count_sentence(ohne["counts"])
        context["without_filters_url"] = params.without_filters().url()
    return context


def period_options(params: SearchQuery, counts: Mapping[str, int], *, custom: bool = False) -> list[dict[str, Any]]:
    """Optionen des Zeitraums: „Beliebig“, die Voreinstellungen und auf Wunsch „Eigener Zeitraum“ (Work)."""
    options: list[dict[str, Any]] = [
        {
            "value": "",
            "label": "Beliebig",
            "count": None,
            "checked": not params.period,
            "url": params.url(period=""),
        },
        *(
            {
                "value": value,
                "label": label,
                "count": format_count(counts.get(value)),
                "checked": value == params.period,
                "url": params.url(period=value),
            }
            for value, label, _von, _bis in PERIODS
        ),
    ]
    if custom:
        options.append(
            {
                "value": PERIOD_CUSTOM,
                "label": "Eigener Zeitraum",
                "count": None,
                "checked": params.period == PERIOD_CUSTOM,
                "url": params.url(period=PERIOD_CUSTOM),
            }
        )
    return options


def _art_url(params: SearchQuery, label: str) -> str:
    """Adresse, die eine Art in der Filterspalte wählt bzw. wieder abwählt (offene Liste ab 2xl, Issue #841)."""
    if label in params.paper_types:
        return params.url(paper_types=[a for a in params.paper_types if a != label])
    return params.url(paper_types=[*params.paper_types, label])


def option_ids(werte: list[str]) -> list[str]:
    """Feste, eindeutige Kurzform je Art für die id der Option in der Filterspalte (Issue #841).

    HTMX setzt den Tastaturfokus nach dem Austausch über die id zurück; die Kurzform hängt daher nur am Wert. Werte,
    die gleich gekürzt würden („Ergänzung“, „Erganzung“), bekommen eine laufende Nummer, damit keine id doppelt ist.
    """
    vergeben: set[str] = set()
    kennungen: list[str] = []
    for wert in werte:
        basis = slugify(wert) or "art"
        kennung, nummer = basis, 2
        while kennung in vergeben:
            kennung, nummer = f"{basis}-{nummer}", nummer + 1
        vergeben.add(kennung)
        kennungen.append(kennung)
    return kennungen


def format_count(value: int | None) -> str | None:
    """Zahl mit Tausenderpunkt („17.639“); ``None`` bleibt ohne Zähler."""
    return None if value is None else f"{int(value):,}".replace(",", ".")


def active_filters(params: SearchQuery) -> list[dict[str, str]]:
    """Gewählte Filter mit der Adresse, die sie wieder entfernt (Zeitraum, Art; in Work dazu Gremium)."""
    aktiv: list[dict[str, str]] = []
    if params.period == PERIOD_CUSTOM and (params.date_from or params.date_to):
        von = german_date(params.date_from) or "Beginn"
        bis = german_date(params.date_to) or "heute"
        aktiv.append({"label": f"Zeitraum: {von} bis {bis}", "url": params.url(period="")})
    for value, label, _von, _bis in PERIODS:
        if value == params.period:
            aktiv.append({"label": f"Zeitraum: {label}", "url": params.url(period="")})
    for art in params.paper_types:
        rest = [a for a in params.paper_types if a != art]
        aktiv.append({"label": f"Art: {art}", "url": params.url(paper_types=rest)})
    if params.paper_type:
        aktiv.append({"label": f"Art: {params.paper_type}", "url": params.url(paper_type="")})
    if params.committee:
        aktiv.append({"label": f"Gremium: {params.committee}", "url": params.url(committee="")})
    return aktiv
