# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aggregator: der RIS-Bestand aller Kommunen als eine OParl-1.1-Ausgabe (``/oparl/v1/``).

Endpunkte (alle rein lesend, anonym, JSON, CORS offen):

- ``/oparl/v1/``                                  Übersicht
- ``/oparl/v1/system``                            System-Objekt
- ``/oparl/v1/bodies``                            externe Liste aller Kommunen
- ``/oparl/v1/body/<uuid>``                       einzelne Kommune
- ``/oparl/v1/body/<uuid>/organizations``         externe Listen je Kommune
- ``/oparl/v1/body/<uuid>/people``                (Blättern, Zeitfilter)
- ``/oparl/v1/body/<uuid>/meetings``
- ``/oparl/v1/body/<uuid>/papers``
- ``/oparl/v1/body/<uuid>/locations``
- ``/oparl/v1/body/<uuid>/changes``               Änderungsfeed (wenn eingeschaltet, ``hub.api.changes``)
- ``/oparl/v1/body/<uuid>/snapshot``              Snapshot mit Cursor-Übergabe (``hub.api.snapshot``)
- ``/oparl/v1/<typ>/<uuid>``                      Objekt-Endpunkte aller Typen

Dieses Modul wählt aus, was sichtbar ist (Kommune, Veröffentlichungsstand, Gelöschtes), und reicht es
weiter: Wie ein Objekt aussieht, legt ``hub.ris.mapping.bestand`` fest, wie eine Liste ausgegeben wird,
``hub.api.serialization``.

- Listen laden nur die nötigen Beziehungen vor (keine Abfrage je Objekt) und sind nach ``modified``
  sortiert – stabil für inkrementelle Abnehmer.
- Seiten ungefilterter Listen liegen kurz im Cache (``OPARL_API_CACHE_SECONDS``).
- In der Quelle Gelöschtes erscheint nur in Listen mit ``modified_since`` (als gekürztes Objekt) und
  bleibt unter seiner Adresse abrufbar (OParl 1.1 §2.8).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Final

from django.conf import settings
from django.core.cache import cache
from django.db.models import Model, QuerySet
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from hub.api import changes, snapshot
from hub.api.http import endpoint, error_response, json_response
from hub.api.serialization import TimeFilters, list_cache_key, list_response, page_number
from hub.ris import selectors as ris
from hub.ris.canonical import Objekt
from hub.ris.mapping import bestand
from hub.ris.mapping.bestand import BestandMapping, RefContext
from insight_core import publication
from insight_core.models import (
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
)

# Zeitfilter -> Vergleich auf den zusammengeführten Zeitstempeln (``_annotated``), damit die Filter genau
# zu den ausgegebenen Werten von ``created``/``modified`` passen
FILTER_LOOKUPS: Final[dict[str, str]] = {
    "created_since": "sort_created__gte",
    "created_until": "sort_created__lte",
    "modified_since": "sort_modified__gte",
    "modified_until": "sort_modified__lte",
}


@dataclass(frozen=True)
class Spec:
    """Ein Objekttyp der Ausgabe: Modell, Abbildung, Vorladen und Auflösung der Verweise je Seite."""

    kind: str
    model: type[Model]
    render: Callable[[BestandMapping, Any, RefContext], Objekt]
    prepare: Callable[[QuerySet[Any]], QuerySet[Any]] | None = None
    context: Callable[[list[Any]], RefContext] = RefContext.empty

    def queryset(self, **filters: Any) -> QuerySet[Any]:
        queryset: QuerySet[Any] = self.model._default_manager.filter(**filters)
        return self.prepare(queryset) if self.prepare else queryset


_BODY = Spec("body", OParlBody, BestandMapping.body, bestand.prepare_bodies)
_ORGANIZATION = Spec("organization", OParlOrganization, BestandMapping.organization, bestand.prepare_organizations)
_PERSON = Spec("person", OParlPerson, BestandMapping.person, bestand.prepare_persons)
_MEETING = Spec("meeting", OParlMeeting, BestandMapping.meeting, bestand.prepare_meetings, RefContext.for_meetings)
_PAPER = Spec("paper", OParlPaper, BestandMapping.paper, bestand.prepare_papers, RefContext.for_papers)
_LOCATION = Spec("location", OParlLocation, BestandMapping.location)

#: Externe Listen je Kommune: Segment der Adresse -> Objekttyp
BODY_LISTS: Final[dict[str, Spec]] = {
    "organizations": _ORGANIZATION,
    "people": _PERSON,
    "meetings": _MEETING,
    "papers": _PAPER,
    # Erweiterung: nicht Teil der Pflichtlisten der Spezifikation
    "locations": _LOCATION,
}

#: Objekt-Endpunkte: Typ in der Adresse -> Objekttyp
OBJECT_TYPES: Final[dict[str, Spec]] = {
    "body": _BODY,
    "organization": _ORGANIZATION,
    "person": _PERSON,
    "membership": Spec("membership", OParlMembership, BestandMapping.membership),
    "meeting": _MEETING,
    "agendaitem": Spec("agendaitem", OParlAgendaItem, BestandMapping.agenda_item, context=RefContext.for_agenda_items),
    "paper": _PAPER,
    "consultation": Spec(
        "consultation", OParlConsultation, BestandMapping.consultation, context=RefContext.for_consultations
    ),
    # Der erkannte Text nur am Objekt-Endpunkt, nicht in Listen und Einbettungen
    "file": Spec("file", OParlFile, BestandMapping.file_with_text),
    "location": _LOCATION,
    "legislativeterm": Spec("legislativeterm", OParlLegislativeTerm, BestandMapping.legislative_term),
}


def mapping() -> BestandMapping:
    """
    Abbildung mit den Adressen dieser Installation (``OPARL_BASE_URL`` und ``SITE_URL``, nicht der Host
    der Anfrage).
    """
    return BestandMapping(
        settings.OPARL_BASE_URL,
        settings.SITE_URL,
        license_url=getattr(settings, "OPARL_LICENSE_URL", ""),
        changes=changes.enabled(),
    )


def _cache_seconds() -> int:
    return int(getattr(settings, "OPARL_API_CACHE_SECONDS", 60))


def _paused_response(body_id: uuid.UUID | str | None) -> HttpResponse | None:
    """
    Vorübergehend abgeschaltete Kommune: 503 mit ``Retry-After`` statt Daten.

    Inkrementelle Abnehmer versuchen es später wieder und behalten ihren Stand; nichts gilt als gelöscht.
    Archiv und dauerhafte Rücknahme brauchen hier nichts Eigenes (lesbar bzw. gekürzte Objekte).
    """
    state = publication.body_state(body_id)
    if state is None or not state.paused:
        return None
    response = error_response(503, "Die Kommune hat die Veröffentlichung vorübergehend abgeschaltet.")
    response["Retry-After"] = str(publication.RETRY_AFTER_SECONDS)
    response["Cache-Control"] = "no-store"
    return response


def _annotated(queryset: QuerySet[Any]) -> QuerySet[Any]:
    """Sortierung und Filter auf den zusammengeführten Zeitstempeln – passend zur Ausgabe."""
    annotated: QuerySet[Any] = queryset.annotate(
        sort_created=Coalesce("oparl_created", "created_at"),
        sort_modified=Coalesce("oparl_modified", "updated_at"),
    ).order_by("sort_modified", "id")
    return annotated


def _list_response(request: HttpRequest, base_url: str, queryset: QuerySet[Any], spec: Spec) -> HttpResponse:
    """
    Externe Liste ausgeben. In der Quelle Gelöschtes erscheint nur in der inkrementellen Liste
    (``modified_since``), als gekürztes Objekt.
    """
    filters = TimeFilters.from_request(request)
    queryset = filters.apply(queryset, FILTER_LOOKUPS)
    if not filters.incremental:
        queryset = queryset.filter(deleted=False)
    output = mapping()

    def render(objects: list[Any]) -> list[Objekt]:
        ctx = spec.context([obj for obj in objects if not obj.deleted])
        return [output.tombstone(spec.kind, obj) if obj.deleted else spec.render(output, obj, ctx) for obj in objects]

    return list_response(request, base_url, queryset, filters, render, cache_seconds=_cache_seconds())


# =============================================================================
# Endpunkte
# =============================================================================


@endpoint
def root_view(request: HttpRequest) -> HttpResponse:
    """Kleine Übersicht über die Schnittstelle (kein OParl-Objekt)."""
    uris = mapping().uris
    return json_response(
        {
            "name": "mandari — aggregierte OParl-Datenquelle",
            "description": (
                "OParl-1.1-konforme, lesende API über die von mandari gespiegelten "
                "Ratsinformationen aller angebundenen Kommunen. Einstieg über das System-Objekt."
            ),
            "oparlVersion": "https://schema.oparl.org/1.1/",
            "system": uris.system(),
            "bodies": uris.bodies(),
            "documentation": "https://github.com/mandariOSS/mandari/blob/main/docs/OPARL_API.md",
            "specification": "https://oparl.org/spezifikation/",
            "objectEndpoints": {kind: uris.obj(kind, "<uuid>") for kind in OBJECT_TYPES},
        }
    )


@endpoint
def system_view(request: HttpRequest) -> HttpResponse:
    return json_response(mapping().system())


@endpoint
def bodies_view(request: HttpRequest) -> HttpResponse:
    # Nicht gelistete Kommunen (z. B. Demo) fehlen in der Liste, bleiben aber unter ihrer Adresse abrufbar
    queryset = _annotated(_BODY.queryset(is_listed=True))
    return _list_response(request, mapping().uris.bodies(), queryset, _BODY)


def _unknown_list(segment: str) -> HttpResponse:
    return error_response(404, f"Unbekannte Liste '{segment}'. Verfügbar: {', '.join(sorted(BODY_LISTS))}.")


@endpoint
def body_sub_list(request: HttpRequest, pk: uuid.UUID, segment: str) -> HttpResponse:
    spec = BODY_LISTS.get(segment)
    if spec is None:
        return _unknown_list(segment)
    base_url = mapping().uris.list(pk, segment)
    paused = _paused_response(pk)
    if paused is not None:
        return paused

    queryset = _annotated(spec.queryset(body_id=pk))

    # Ob es die Kommune gibt, prüft nur der Weg ohne Cache-Treffer
    cached = (
        not TimeFilters.requested(request)
        and _cache_seconds()
        and cache.get(list_cache_key(base_url, page_number(request)))
    )
    if not cached and not OParlBody.objects.filter(pk=pk).exists():
        return error_response(404, "Kommune (Body) nicht gefunden.")

    return _list_response(request, base_url, queryset, spec)


def _existing(kind: str, ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """
    Kennungen, unter denen der Bestand ein Objekt des Typs ausliefert – auch Gelöschtes, das als
    gekürztes Objekt abrufbar bleibt. Ein Ort ohne eigenes Objekt der Quelle trägt die Kennung seiner
    Sitzung (``_meeting_location_response``).
    """
    spec = OBJECT_TYPES.get(kind)
    if spec is None or not ids:
        return set()
    found: set[uuid.UUID] = set(spec.model._default_manager.filter(pk__in=ids).values_list("pk", flat=True))
    if kind == "location" and len(found) < len(ids):
        rest = [object_id for object_id in ids if object_id not in found]
        found.update(OParlMeeting.objects.filter(pk__in=rest).values_list("pk", flat=True))
    return found


def _feed(output: BestandMapping, pk: uuid.UUID) -> changes.Feed:
    uris = output.uris

    def addresses(kind: str, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
        # Die Kennung im Ereignis ist die des Bestands, und der Bestand enthält nur Öffentliches. Gibt es
        # das Objekt nicht (noch nicht übernommen oder fälschlich als öffentlich gemeldet), gibt es keinen
        # Eintrag – wie bei der Session-Schnittstelle
        return {object_id: uris.obj(kind, object_id) for object_id in _existing(kind, ids)}

    return changes.Feed(body_id=pk, url=uris.changes(pk), snapshot_url=uris.snapshot(pk), addresses=addresses)


def _feed_unavailable(pk: uuid.UUID, segment: str) -> HttpResponse | None:
    """Warum Feed oder Snapshot einer Kommune nicht geliefert werden (sonst ``None``)."""
    if not changes.enabled():
        # Ausgeschaltet gibt es die Adresse nicht
        return _unknown_list(segment)
    paused = _paused_response(pk)
    if paused is not None:
        return paused
    if not OParlBody.objects.filter(pk=pk).exists():
        return error_response(404, "Kommune (Body) nicht gefunden.")
    return None


@endpoint
def body_changes(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Änderungsfeed einer Kommune (``hub.api.changes``)."""
    unavailable = _feed_unavailable(pk, "changes")
    if unavailable is not None:
        return unavailable
    return changes.changes_response(request, _feed(mapping(), pk))


def _section(output: BestandMapping, spec: Spec, pk: uuid.UUID) -> snapshot.Section:
    def render(objects: list[Any]) -> list[Objekt]:
        ctx = spec.context(objects)
        return [spec.render(output, obj, ctx) for obj in objects]

    return snapshot.Section(queryset=spec.queryset(body_id=pk, deleted=False), render=render)


@endpoint
def body_snapshot(request: HttpRequest, pk: uuid.UUID) -> HttpResponseBase:
    """Snapshot einer Kommune mit Cursor-Übergabe (``hub.api.snapshot``): dieselben Objekte wie die Listen."""
    unavailable = _feed_unavailable(pk, "snapshot")
    if unavailable is not None:
        return unavailable
    output = mapping()

    def body() -> Objekt:
        obj = _BODY.queryset(pk=pk).get()
        # Eine zurückgenommene Kommune steht auch im Snapshot nur als gekürztes Objekt
        return output.tombstone("body", obj) if obj.deleted else output.body(obj)

    sections = [_section(output, spec, pk) for spec in BODY_LISTS.values()]
    return snapshot.snapshot_response(request, snapshot.Snapshot(_feed(output, pk), body, sections))


def _meeting_location_response(output: BestandMapping, pk: uuid.UUID) -> HttpResponse:
    """
    Sitzungsort ohne eigenes Location-Objekt der Quelle: ``…/location/<Kennung der Sitzung>``
    (``BestandMapping.meeting_location``). Entfällt die Ortsangabe oder die Sitzung, bleibt die Adresse
    als gekürztes Objekt mit ``"deleted": true`` abrufbar (OParl 1.1 §2.8).
    """
    meeting = ris.meeting_by_id(pk)
    if meeting is None:
        return error_response(404, f"{output.uris.obj('location', pk)} nicht gefunden.")
    if not meeting.deleted and publication.states():
        paused = _paused_response(meeting.body_id)
        if paused is not None:
            return paused
    data = None if meeting.deleted else output.meeting_location(meeting)
    if data is None:
        data = output.tombstone("location", meeting)
    return json_response(data)


@endpoint
def object_view(request: HttpRequest, kind: str, pk: uuid.UUID) -> HttpResponse:
    kind = kind.lower()
    spec = OBJECT_TYPES.get(kind)
    if spec is None:
        return error_response(404, f"Unbekannter Objekttyp '{kind}'. Verfügbar: {', '.join(sorted(OBJECT_TYPES))}.")
    output = mapping()
    obj = spec.queryset(pk=pk).first()
    if obj is None:
        if kind == "location":
            return _meeting_location_response(output, pk)
        return error_response(404, f"{output.uris.obj(kind, pk)} nicht gefunden.")
    if obj.deleted:
        # OParl 1.1 §2.8: Gelöschtes bleibt unter seiner Adresse abrufbar – als gekürztes Objekt mit
        # "deleted": true und HTTP 200
        return json_response(output.tombstone(kind, obj))
    if publication.states():
        paused = _paused_response(publication.body_id_of(spec.model, pk))
        if paused is not None:
            return paused
    return json_response(spec.render(output, obj, spec.context([obj])))
