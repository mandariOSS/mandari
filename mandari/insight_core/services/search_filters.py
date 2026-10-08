# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Parameter der Suche (Konzept Insight-Suche, P1.12; Issue #853): ein Satz für Work und das Bürgerportal.

``SearchQuery`` lag bis 10/2026 in ``apps.work.ris.services`` und kannte nur die Work-Parameter; dort bleibt der Name
als Verweis erhalten. Hier versteht die Klasse beide Schreibweisen, damit alte Links weiter gelten:

========================  ==================================  ===========================================
Parameter                 Bedeutung                           ältere Schreibweise
========================  ==================================  ===========================================
``q``                     Suchbegriff
``result_type``           Reiter (Vorgänge, Sitzungen …)       ``typ`` (Work), ``type`` (Bürgerportal)
``period``                Zeitraum: 12m, 2y, 5y, older, frei
``von``, ``bis``          eigener Zeitraum (ISO-Datum)          nur mit ``period=frei``; ohne ``period`` gilt frei
``paper_type``            Art, zusammengefasst, mehrfach       (Bürgerportal)
``art``                   Art als Wert aus dem RIS (Work)
``gremium``               Name eines Gremiums (Work)
``kommune``               Kennung einer Kommune (Work, mehrere Kommunen)
``sort``                  ``relevance`` oder ``newest``
``page``                  Seite                               ``seite`` (Work)
``ort``                   ``aus`` = Nur Wortsuche (Bürgerportal)
========================  ==================================  ===========================================

Das Bürgerportal liest dieselbe Klasse mit ``from_get(…, portal=True)``: Gremium, Art als Originalwert, Kommune und
eigener Zeitraum bietet es nicht an, dort bleiben sie leer (``search_page.SearchParams`` ist derselbe Name).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final

from django.utils.http import urlencode

if TYPE_CHECKING:
    from django.http import QueryDict

RESULT_TYPES: Final = ("papers", "meetings", "persons", "organizations", "files")
#: Alte Werte von ``type`` im Bürgerportal (bis 10/2026) → ``result_type``
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
#: Eigener Zeitraum (``von``/``bis``, Work); nicht in ``PERIODS``, weil das Bürgerportal ihn nicht anbietet
PERIOD_CUSTOM: Final = "frei"
SORTS: Final = (("relevance", "Relevanz"), ("newest", "Neueste"))
MAX_QUERY_LENGTH: Final = 200


def _iso_date(value: str | None) -> str:
    """ISO-Datum oder leer (ungültige Eingaben fallen weg, statt die Suche scheitern zu lassen)."""
    text = (value or "").strip()
    try:
        return date.fromisoformat(text).isoformat() if text else ""
    except ValueError:
        return ""


def _page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


@dataclass
class SearchQuery:
    """Vom Nutzer gesetzte Suchparameter (bereinigt); Feldnamen wie bis 10/2026 in Work, dazu Zeitraum, Art, Sortierung."""

    query: str = ""
    date_from: str = ""
    date_to: str = ""
    committee: str = ""
    #: Art als Originalwert aus dem RIS (Parameter ``art``)
    paper_type: str = ""
    result_type: str = ""
    body_filter: str = ""
    page: int = 1
    period: str = ""
    #: Arten, zusammengefasst (``normalize_paper_type``, Parameter ``paper_type``, mehrfach)
    paper_types: list[str] = field(default_factory=list)
    sort: str = "relevance"
    #: Ortsband (Bürgerportal); ``ort=aus`` heißt „Nur Wortsuche“
    ort: bool = True

    @classmethod
    def from_get(cls, get: QueryDict, *, portal: bool = False) -> SearchQuery:
        """
        Aus den Parametern einer Anfrage; versteht die alten Schreibweisen (``typ``, ``seite``, ``type``).

        ``portal=True``: nur die Parameter der Suchseite des Bürgerportals (``q``, ``result_type``/``type``,
        ``period``, ``paper_type``, ``sort``, ``page``, ``ort``); die Filter aus Work bleiben leer.
        """
        work = not portal
        result_type = (
            get.get("result_type") or (get.get("typ") if work else "") or LEGACY_TYPES.get(get.get("type", ""), "")
        )
        period = get.get("period", "")
        if period not in {p[0] for p in PERIODS} | ({PERIOD_CUSTOM} if work else set()):
            period = ""
        von, bis = (_iso_date(get.get("von")), _iso_date(get.get("bis"))) if work else ("", "")
        if (von or bis) and not period:
            period = PERIOD_CUSTOM  # alter Work-Link mit von/bis
        if period != PERIOD_CUSTOM:
            von = bis = ""  # eine Voreinstellung ersetzt den eigenen Zeitraum
        sort = get.get("sort", "")
        return cls(
            query=" ".join(get.get("q", "").split())[:MAX_QUERY_LENGTH],
            date_from=von,
            date_to=bis,
            committee=get.get("gremium", "").strip()[:MAX_QUERY_LENGTH] if work else "",
            paper_type=get.get("art", "").strip()[:MAX_QUERY_LENGTH] if work else "",
            result_type=result_type if result_type in RESULT_TYPES else "",
            body_filter=get.get("kommune", "").strip()[:64] if work else "",
            page=_page(get.get("page") or (get.get("seite") if work else None)),
            period=period,
            paper_types=[v for v in dict.fromkeys(get.getlist("paper_type")) if v][:20],
            sort=sort if sort in {s[0] for s in SORTS} else "relevance",
            ort=get.get("ort", "") != "aus",
        )

    @property
    def has_filters(self) -> bool:
        """Wirkt ein Filter? „Eigener Zeitraum“ ohne Datum zählt nicht."""
        period = self.period if self.period != PERIOD_CUSTOM else ""
        return any(
            [
                self.date_from,
                self.date_to,
                self.committee,
                self.paper_type,
                self.body_filter,
                period,
                self.paper_types,
            ]
        )

    @property
    def is_empty(self) -> bool:
        return not self.query and not self.has_filters

    def without_filters(self) -> SearchQuery:
        """Dieselbe Suche ohne Filter des Inhalts; die Kommune bleibt (sie ist der Bereich der Suche)."""
        return replace(self, period="", date_from="", date_to="", paper_types=[], paper_type="", committee="", page=1)

    def index_names(self) -> list[str] | None:
        """Elasticsearch-Indexe, auf die die Filter wirken (``None`` = alle)."""
        if self.result_type in RESULT_TYPES:
            return [self.result_type]
        if self.committee or self.paper_type:
            # Diese Filter wirken nur auf Dokument-/Sitzungs-Indexe
            return ["papers"] if self.paper_type else ["papers", "meetings", "files"]
        return None

    def date_range(self, today: date) -> tuple[str | None, str | None]:
        """Zeitraum als ISO-Daten: eigener Zeitraum (``von``/``bis``) oder die Voreinstellung ``period``."""
        if self.period == PERIOD_CUSTOM or ((self.date_from or self.date_to) and not self.period):
            return self.date_from or None, self.date_to or None
        for value, _label, back_from, back_to in PERIODS:
            if value == self.period:
                von = (today - timedelta(days=back_from)).isoformat() if back_from else None
                bis = (today - timedelta(days=back_to)).isoformat() if back_to else None
                return von, bis
        return None, None

    def url(self, **changes: Any) -> str:
        """Adresse mit geänderten Parametern (Seite beginnt wieder bei 1, leere Werte entfallen)."""
        neu = replace(self, **{"page": 1, **changes})
        eigener = neu.period == PERIOD_CUSTOM
        teile: list[tuple[str, str]] = [("q", neu.query)]
        for name, value in (
            ("result_type", neu.result_type),
            ("period", neu.period),
            ("von", neu.date_from if eigener else ""),
            ("bis", neu.date_to if eigener else ""),
        ):
            if value:
                teile.append((name, value))
        teile += [("paper_type", v) for v in neu.paper_types]
        for name, value in (("art", neu.paper_type), ("gremium", neu.committee), ("kommune", neu.body_filter)):
            if value:
                teile.append((name, value))
        if neu.sort != "relevance":
            teile.append(("sort", neu.sort))
        if not neu.ort:
            teile.append(("ort", "aus"))
        if neu.page > 1:
            teile.append(("page", str(neu.page)))
        return "?" + urlencode(teile)
