# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchseite des Bürgerportals (Konzept Insight-Suche, P0.8/P0.9): Parameter, Reiter, Filter, Ortsband.

Parameter heißen wie in ``apps.work.ris.services.SearchQuery`` (``q``, ``result_type``, ``date_from``,
``date_to``, ``paper_type``, ``page``), dazu ``period`` (Voreinstellung des Zeitraums), ``sort`` und ``ort=aus``
(„Nur Wortsuche“). So lassen sich Filterleiste und Parameter später in Work übernehmen (P1.12); alte Links mit
``type=paper`` gelten weiter. Die Seite fragt Elasticsearch einmal für Liste, Zahl und alle Reiter
(``search_grouped`` mit Gewichten je Reiter) und einmal für die Zähler der Filter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final

from django.utils.http import urlencode

from .search_presentation import count_sentence, normalize_paper_type, present_groups

if TYPE_CHECKING:
    from django.http import QueryDict

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
#: Alte Werte von ``type`` (bis 10/2026) → ``result_type``
LEGACY_TYPES: Final = {
    "all": "",
    "paper": "papers",
    "meeting": "meetings",
    "file": "files",
    "person": "persons",
    "organization": "organizations",
}
#: Zeitraum: Wert, Beschriftung, Tage zurück für „ab“ bzw. „bis“
PERIODS: Final = (
    ("12m", "Letzte 12 Monate", 365, None),
    ("2y", "Letzte 2 Jahre", 730, None),
    ("5y", "Letzte 5 Jahre", 1826, None),
    ("older", "Älter als 5 Jahre", None, 1826),
)
SORTS: Final = (("relevance", "Relevanz"), ("newest", "Neueste"))
PAGE_SIZE: Final = 20


@dataclass
class SearchParams:
    """Bereinigte Parameter der Suchseite."""

    q: str = ""
    result_type: str = ""
    period: str = ""
    paper_types: list[str] = field(default_factory=list)
    sort: str = "relevance"
    page: int = 1
    ort: bool = True

    @classmethod
    def from_get(cls, get: QueryDict) -> SearchParams:
        result_type = get.get("result_type") or LEGACY_TYPES.get(get.get("type", ""), "")
        try:
            page = max(1, int(get.get("page", "1")))
        except ValueError:
            page = 1
        return cls(
            q=" ".join(get.get("q", "").split())[:200],
            result_type=result_type if result_type in {t[0] for t in TABS} else "",
            period=get.get("period", "") if get.get("period", "") in {p[0] for p in PERIODS} else "",
            paper_types=[v for v in dict.fromkeys(get.getlist("paper_type")) if v][:20],
            sort=get.get("sort", "") if get.get("sort", "") in {s[0] for s in SORTS} else "relevance",
            page=page,
            ort=get.get("ort", "") != "aus",
        )

    @property
    def has_filters(self) -> bool:
        return bool(self.period or self.paper_types)

    def date_range(self, today: date) -> tuple[str | None, str | None]:
        for value, _label, back_from, back_to in PERIODS:
            if value == self.period:
                von = (today - timedelta(days=back_from)).isoformat() if back_from else None
                bis = (today - timedelta(days=back_to)).isoformat() if back_to else None
                return von, bis
        return None, None

    def url(self, **changes: Any) -> str:
        """Adresse der Suchseite mit geänderten Parametern (Seite beginnt wieder bei 1)."""
        neu = replace(self, **{"page": 1, **changes})
        teile: list[tuple[str, str]] = [("q", neu.q)]
        if neu.result_type:
            teile.append(("result_type", neu.result_type))
        if neu.period:
            teile.append(("period", neu.period))
        teile += [("paper_type", v) for v in neu.paper_types]
        if neu.sort != "relevance":
            teile.append(("sort", neu.sort))
        if not neu.ort:
            teile.append(("ort", "aus"))
        if neu.page > 1:
            teile.append(("page", str(neu.page)))
        return "?" + urlencode(teile)


def papers_of_types(labels: list[str], body_slug: str) -> Callable[[set[str]], set[str]]:
    """Filter für Dateien: Vorgänge, deren normalisierte Art gewählt ist (eine Datenbankabfrage je Suche)."""

    def erlaubt(paper_ids: set[str]) -> set[str]:
        import uuid

        from insight_core.models import OParlPaper

        gewaehlt = set(labels)
        kennungen = []
        for pid in paper_ids:
            try:
                kennungen.append(uuid.UUID(pid))
            except ValueError:
                continue
        rows = OParlPaper.objects.filter(pk__in=kennungen).values_list("pk", "paper_type")
        return {str(pk) for pk, art in rows if normalize_paper_type(art, body_slug) in gewaehlt}

    return erlaubt


def build_context(
    service: Any, params: SearchParams, body: OParlBody | None, body_ids: list[str] | None, today: date
) -> dict[str, Any]:
    """Alles, was Seite und Austausch per HTMX brauchen: Liste, Zahl, Reiter, Filter, Ortsband."""
    from .search_places import detect_place, place_band

    slug = (body.slug or "") if body is not None else ""
    body_id = str(body.pk) if body is not None else None
    date_from, date_to = params.date_range(today)
    common = {"body_id": body_id, "body_ids": body_ids if body is None else None}

    facets = service.facet_counts(params.q, date_from=date_from, date_to=date_to, **common)
    art_counts: dict[str, int] = {}
    raw_by_label: dict[str, list[str]] = {}
    for raw, n in facets["paper_types"].items():
        label = normalize_paper_type(raw, slug) or raw
        art_counts[label] = art_counts.get(label, 0) + n
        raw_by_label.setdefault(label, []).append(raw)
    paper_type: list[str] | None = None
    if params.paper_types:
        paper_type = [raw for label in params.paper_types for raw in raw_by_label.get(label, [])] or ["∅"]

    tab = next(t for t in TABS if t[0] == params.result_type)
    kinds = tab[3]
    if params.paper_types:
        # „Art“ gibt es nur für Vorgänge: Sitzungen und Unterlagen ohne Vorgang fallen mit dem Filter heraus
        kinds = {"paper"} if kinds is None else kinds & {"paper"}
    grouped = service.search_grouped(
        params.q,
        page=params.page,
        page_size=PAGE_SIZE,
        date_from=date_from,
        date_to=date_to,
        paper_type=paper_type,
        sort=params.sort,
        weights=tab[2],
        kinds=kinds,
        file_paper_filter=papers_of_types(params.paper_types, slug) if params.paper_types else None,
        **common,
    )
    counts: Mapping[str, Any] = grouped["counts"]
    totals: Mapping[str, int] = grouped["totals_by_index"]
    groups = present_groups(grouped["groups"], slug)

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
            "count": _zahl(tab_counts[key]),
            "url": params.url(result_type=key),
            "active": key == params.result_type,
        }
        for key, label, _weights, _kinds in TABS
        if key == "" or key == params.result_type or tab_counts[key]
    ]

    band = None
    place = None
    if params.page == 1 and params.ort and body is not None and params.q and not params.has_filters:
        place = detect_place(body, params.q)
        if place is not None:
            band = place_band(body, place, params.q)

    context: dict[str, Any] = {
        "params": params,
        "query": params.q,
        "groups": groups,
        "count_sentence": count_sentence(counts),
        "similar_spelling": grouped["similar_spelling"],
        "has_more": grouped["has_more"],
        "page": params.page,
        "next_url": params.url(page=params.page + 1),
        "tabs": tabs,
        "art_options": [
            {"value": label, "count": _zahl(n), "checked": label in params.paper_types}
            for label, n in sorted(art_counts.items(), key=lambda item: -item[1])
        ][:12],
        "period_options": [
            {"value": "", "label": "Beliebig", "count": None, "checked": not params.period},
            *(
                {
                    "value": value,
                    "label": label,
                    "count": _zahl(facets["periods"].get(value)),
                    "checked": value == params.period,
                }
                for value, label, _von, _bis in PERIODS
            ),
        ],
        "sort_options": [{"value": v, "label": label, "checked": v == params.sort} for v, label in SORTS],
        "active_filters": _active_filters(params),
        "place_band": band,
        "place_name": place.name if place is not None else "",
        "word_only_url": params.url(ort=False) if band else "",
        "place_url": params.url(ort=True) if not params.ort else "",
    }
    if not groups and params.has_filters:
        ohne = service.search_grouped(params.q, page=1, page_size=1, weights=tab[2], kinds=tab[3], **common)
        context["without_filters_count"] = count_sentence(ohne["counts"])
        context["without_filters_url"] = params.url(period="", paper_types=[])
    return context


def _zahl(value: int | None) -> str | None:
    """Zahl mit Tausenderpunkt („17.639“); ``None`` bleibt ohne Zähler."""
    return None if value is None else f"{int(value):,}".replace(",", ".")


def _active_filters(params: SearchParams) -> list[dict[str, str]]:
    aktiv: list[dict[str, str]] = []
    for value, label, _von, _bis in PERIODS:
        if value == params.period:
            aktiv.append({"label": f"Zeitraum: {label}", "url": params.url(period="")})
    for art in params.paper_types:
        rest = [a for a in params.paper_types if a != art]
        aktiv.append({"label": f"Art: {art}", "url": params.url(paper_types=rest)})
    return aktiv
