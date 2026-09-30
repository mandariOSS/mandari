# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die eine Serialisierung der offenen Schnittstelle: aus kanonischen Objekten werden Antworten.

Aggregator und Session-Schnittstelle bilden ihre Objekte mit ``hub.ris.mapping`` auf das kanonische
Modell ab und geben sie hier aus. Was eine externe Liste ist, steht deshalb an genau einer Stelle:

- **Zeitfilter** (``TimeFilters``): ``created_since``, ``created_until``, ``modified_since`` und
  ``modified_until`` mit Pflicht zur Zeitzone. Eine Liste mit ``modified_since`` ist *inkrementell*
  und enthält auch Gelöschtes als gekürzte Objekte (OParl 1.1 §2.8); alle anderen Listen nicht.
- **Blättern** (``page_number``, ``page_size``) und **Listen-Hülle** (``list_envelope``):
  ``data``/``pagination``/``links`` samt ``Link``-Header; die Filter der Anfrage bleiben in den
  Blätter-Links erhalten.
- **Gelöschtes neben Bestehendem** (``MergedEntries``): Führt eine Ausgabe Gelöschtes in einer eigenen
  Tabelle, entsteht die inkrementelle Liste durch Zusammenführen nach ``modified`` – geladen werden nur
  die Objekte der Seite.
- **Antwort** (``list_response``): eine Seite abbilden und ausgeben, auf Wunsch kurz zwischengespeichert.

Die Ausgabe selbst entscheidet nur, welche Einträge sichtbar sind (``entries``) und wie ein Eintrag als
kanonisches Objekt aussieht (``render``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Page, Paginator
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from hub.api.http import BadRequestError, error_response, json_response, parse_client_datetime
from hub.ris.canonical import Objekt

#: Zeitfilter externer Listen, in der Reihenfolge ihrer Prüfung
TIME_FILTERS: Final[tuple[str, ...]] = ("created_since", "created_until", "modified_since", "modified_until")

#: Eine Seite Einträge abbilden: ein Aufruf je Seite, damit Verweise gesammelt aufgelöst werden können
Render = Callable[[list[Any]], list[Objekt]]


# =============================================================================
# Zeitfilter
# =============================================================================


@dataclass(frozen=True)
class TimeFilters:
    """
    Zeitfilter einer Anfrage.

    ``sent`` hält die Werte, wie der Abnehmer sie geschickt hat (sie bleiben in den Blätter-Links
    erhalten), ``parsed`` die Zeitpunkte.
    """

    sent: Mapping[str, str]
    parsed: Mapping[str, datetime]

    @classmethod
    def from_request(cls, request: HttpRequest) -> TimeFilters:
        """Filter der Anfrage; ein Wert ohne Zeitzone oder kein Zeitpunkt ergibt ``BadRequestError``."""
        sent = {name: request.GET[name] for name in TIME_FILTERS if name in request.GET}
        return cls(sent=sent, parsed={name: parse_client_datetime(value, name) for name, value in sent.items()})

    @staticmethod
    def requested(request: HttpRequest) -> bool:
        """Nennt die Anfrage einen Zeitfilter (ohne ihn zu prüfen)?"""
        return any(name in request.GET for name in TIME_FILTERS)

    @property
    def incremental(self) -> bool:
        """Inkrementelle Liste (``modified_since``): Sie enthält auch Gelöschtes als gekürzte Objekte."""
        return "modified_since" in self.parsed

    def __bool__(self) -> bool:
        return bool(self.sent)

    def apply[Q: QuerySet[Any]](self, queryset: Q, lookups: Mapping[str, str]) -> Q:
        """Filter auf ein QuerySet anwenden; ``lookups`` nennt je Filter den Vergleich auf dessen Feldern."""
        for name, value in self.parsed.items():
            queryset = queryset.filter(**{lookups[name]: value})
        return queryset


# =============================================================================
# Blättern und Listen-Hülle
# =============================================================================


def page_number(request: HttpRequest) -> int:
    """Seitennummer aus ``?page=`` (ab 1); alles andere ergibt eine 400 mit klarer Meldung."""
    raw = request.GET.get("page", "1")
    try:
        number = int(raw)
    except ValueError:
        raise BadRequestError(f"Parameter 'page': '{raw}' ist keine gültige Seitennummer.") from None
    if number < 1:
        raise BadRequestError("Parameter 'page': Seitennummern beginnen bei 1.")
    return number


def page_size() -> int:
    """Objekte je Listen-Seite (``OPARL_API_PAGE_SIZE``)."""
    return int(getattr(settings, "OPARL_API_PAGE_SIZE", 100))


def list_envelope(
    base_url: str, filters: Mapping[str, str], paginator: Paginator[Any], page: Page[Any], data: Sequence[Objekt]
) -> tuple[Objekt, dict[str, str]]:
    """
    Hülle einer externen Objektliste samt ``Link``-Header: ``(envelope, headers)``.

    ``filters`` sind die Zeitfilter der Anfrage, wie der Abnehmer sie geschickt hat; sie bleiben in den
    Blätter-Links erhalten. Seite 1 hat keinen ``page``-Parameter (eine Schreibweise je Adresse).
    """

    def page_link(number: int) -> str:
        params: dict[str, Any] = dict(filters)
        if number > 1:
            params["page"] = number
        return f"{base_url}?{urlencode(params)}" if params else base_url

    links = {"first": page_link(1), "self": page_link(page.number)}
    if page.has_previous():
        links["prev"] = page_link(page.number - 1)
    if page.has_next():
        links["next"] = page_link(page.number + 1)
    links["last"] = page_link(paginator.num_pages)

    envelope = {
        "data": data,
        "pagination": {
            "totalElements": paginator.count,
            "elementsPerPage": paginator.per_page,
            "currentPage": page.number,
            "totalPages": paginator.num_pages,
        },
        "links": links,
    }
    headers = {"Link": ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items() if rel != "self")}
    return envelope, headers


def single_page(base_url: str, data: Sequence[Objekt]) -> Objekt:
    """
    Hülle einer Liste, die immer auf eine Seite passt (etwa die eine Körperschaft eines Mandanten):
    dieselbe Form wie jede externe Liste, ohne Blättern.
    """
    paginator: Paginator[Objekt] = Paginator(list(data), page_size())
    envelope, _ = list_envelope(base_url, {}, paginator, paginator.page(1), data)
    return envelope


# =============================================================================
# Gelöschtes neben Bestehendem
# =============================================================================


@dataclass(frozen=True)
class Gone:
    """Eintrag einer inkrementellen Liste für ein gelöschtes oder nicht mehr öffentliches Objekt."""

    record: Any


class MergedEntries:
    """
    Objekte und Einträge für Gelöschtes als eine nach ``(modified, Quelle, Kennung)`` sortierte Folge für
    den Paginator – ohne eine der Tabellen ganz zu laden.

    Für eine Seite ``[start:stop]`` liest jede Quelle nur Zeitstempel und Kennung ihrer ersten ``stop``
    Einträge (in derselben Sortierung wie hier), die Seite entsteht durch Zusammenführen; vollständig
    geladen werden nur die Einträge der Seite – mit Select/Prefetch des QuerySets. Gelöschtes kommt als
    ``Gone`` zurück.
    """

    def __init__(
        self,
        objects: QuerySet[Any],
        gone: QuerySet[Any],
        *,
        modified: str = "updated_at",
        deleted: str = "deleted_at",
    ) -> None:
        self.modified = modified
        self.deleted = deleted
        self.objects = objects.order_by(modified, "pk")
        self.gone = gone.order_by(deleted, "pk")

    def count(self) -> int:
        return self.objects.count() + self.gone.count()

    def __len__(self) -> int:
        return self.count()

    def __getitem__(self, index: slice) -> list[Any]:
        if not isinstance(index, slice):
            raise TypeError("Nur Ausschnitte (Seiten) werden unterstützt.")
        start, stop = index.start or 0, index.stop
        keys = [(stamp, 0, pk) for stamp, pk in self.objects.values_list(self.modified, "pk")[:stop]]
        keys += [(stamp, 1, pk) for stamp, pk in self.gone.values_list(self.deleted, "pk")[:stop]]
        keys.sort()
        window = keys[start:stop]
        objects = {obj.pk: obj for obj in self.objects.filter(pk__in=[pk for _, src, pk in window if src == 0])}
        gone = {entry.pk: entry for entry in self.gone.filter(pk__in=[pk for _, src, pk in window if src == 1])}
        return [objects[pk] if src == 0 else Gone(gone[pk]) for _, src, pk in window]


# =============================================================================
# Antwort einer externen Liste
# =============================================================================


def list_cache_key(base_url: str, number: int) -> str:
    """Schlüssel, unter dem eine ungefilterte Listen-Seite zwischengespeichert ist."""
    return f"oparl_api:list:{base_url}:p{number}"


def list_response(
    request: HttpRequest,
    base_url: str,
    entries: Any,
    filters: TimeFilters,
    render: Render,
    *,
    cache_seconds: int = 0,
) -> HttpResponse:
    """
    Eine Seite einer externen Liste als Antwort (``data``/``pagination``/``links`` mit ``Link``-Header).

    ``entries`` sind die sichtbaren Einträge, bereits gefiltert und sortiert: ein QuerySet oder
    ``MergedEntries``. ``render`` bildet die Einträge der Seite auf kanonische Objekte ab. Eine Seite
    hinter der letzten ergibt 404.

    Mit ``cache_seconds`` wird die Seite einer Liste ohne Zeitfilter so lange zwischengespeichert
    (samt ``Link``-Header); gefilterte Listen nie.
    """
    number = page_number(request)
    cache_key = list_cache_key(base_url, number) if cache_seconds and not filters else None
    if cache_key:
        cached = cache.get(cache_key)
        if cached is not None:
            return json_response(cached["envelope"], headers=cached["headers"])

    paginator: Paginator[Any] = Paginator(entries, page_size())
    if number > paginator.num_pages:
        return error_response(404, f"Seite {number} existiert nicht (letzte Seite: {paginator.num_pages}).")
    page = paginator.page(number)
    envelope, headers = list_envelope(base_url, filters.sent, paginator, page, render(list(page.object_list)))
    if cache_key:
        cache.set(cache_key, {"envelope": envelope, "headers": headers}, cache_seconds)
    return json_response(envelope, headers=headers)
