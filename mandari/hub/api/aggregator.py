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
- ``/oparl/v1/body/<uuid>/kommparl/changes``      Änderungsfeed nach kommParl (wenn eingeschaltet, ``hub.api.changes``)
- ``/oparl/v1/body/<uuid>/kommparl/snapshot``     Snapshot nach kommParl mit Cursor-Übergabe (``hub.api.snapshot``)
- ``/oparl/v1/body/<uuid>/changes``               Änderungsfeed in der älteren Form (abgekündigt)
- ``/oparl/v1/body/<uuid>/snapshot``              Snapshot in der älteren Form (abgekündigt)
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
from django.db.models import Model, Q, QuerySet
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from hub.api import changes, kommparl, snapshot
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
    # Der erkannte Text nur am Objekt-Endpunkt, nicht in Listen und Einbettungen, und höchstens bis zur Obergrenze
    "file": Spec("file", OParlFile, BestandMapping.file_with_text, bestand.prepare_files),
    "location": _LOCATION,
    "legislativeterm": Spec("legislativeterm", OParlLegislativeTerm, BestandMapping.legislative_term),
}


def mapping() -> BestandMapping:
    """
    Abbildung mit den Adressen dieser Installation (``OPARL_BASE_URL`` und ``SITE_URL``, nicht der Host
    der Anfrage).
    """
    offered = changes.enabled()
    return BestandMapping(
        settings.OPARL_BASE_URL,
        settings.SITE_URL,
        license_url=getattr(settings, "OPARL_LICENSE_URL", ""),
        changes=offered,
        conforms_to=kommparl.conforms_to(feed_offered=offered),
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


def _current(kind: str, ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """
    Kennungen, unter denen der Bestand derzeit ein Objekt ausliefert, ohne Gelöschtes (``_existing`` ohne gekürzte
    Objekte). Ein Ort ohne eigenes Objekt der Quelle ist der einer Sitzung mit Ortsangabe.
    """
    spec = OBJECT_TYPES.get(kind)
    if spec is None or not ids:
        return set()
    found: set[uuid.UUID] = set(
        spec.model._default_manager.filter(pk__in=ids, deleted=False).values_list("pk", flat=True)
    )
    if kind == "location" and len(found) < len(ids):
        rest = [object_id for object_id in ids if object_id not in found]
        found.update(bestand.meetings_with_own_location(rest, deleted=False))
    return found


#: Eingebettete Typen -> (Modell, (Fremdschlüssel, Typ des einbettenden Objekts) …) im Bestand
_EMBEDDED_IN: Final[dict[str, tuple[type[Model], tuple[tuple[str, str], ...]]]] = {
    "agendaitem": (OParlAgendaItem, (("meeting_id", "meeting"),)),
    "consultation": (OParlConsultation, (("paper_id", "paper"),)),
    "file": (OParlFile, (("paper_id", "paper"), ("meeting_id", "meeting"))),
    "membership": (OParlMembership, (("person_id", "person"),)),
}


def _related(pk: uuid.UUID) -> changes.Related:
    """
    Was der Bestand über die Einbettungen weiß und die Ereignisse nicht nennen (``changes.Related``, Issue #1229), je
    Aufruf eine Abfrage:

    - der Sitzungsort einer Sitzung, der ihre Kennung trägt (``BestandMapping.meeting_location``): ändert sich mit
      ihr; ohne Ortsangabe gibt es ihn nicht (mehr),
    - die Sitzungen der Kommune, die einen Ort einbetten (``bestand.meetings_embedding_locations``),
    - Sitzung, Vorlage bzw. Person eines Tagesordnungspunkts, einer Beratung, Datei oder Mitgliedschaft; der Bestand
      behält auch gelöschte Zeilen.
    """

    def related(kind: str, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, list[changes.Link]]:
        found: dict[uuid.UUID, list[changes.Link]] = {}
        if kind == "meeting":
            located = bestand.meetings_with_own_location(ids)
            for object_id in ids:
                link = changes.Link("location", object_id, shared=True, removed=object_id not in located)
                found[object_id] = [link]
        elif kind == "location":
            for location_id, meetings in bestand.meetings_embedding_locations(pk, ids).items():
                found[location_id] = [changes.Link("meeting", meeting_id) for meeting_id in meetings]
        elif kind in _EMBEDDED_IN:
            model, references = _EMBEDDED_IN[kind]
            fields = [name for name, _ in references]
            for object_id, *parents in model._default_manager.filter(pk__in=ids).values_list("pk", *fields):
                found[object_id] = [
                    changes.Link(parent_kind, parent)
                    for (_, parent_kind), parent in zip(references, parents, strict=True)
                    if parent is not None
                ]
        return found

    return related


def _feed(
    output: BestandMapping,
    pk: uuid.UUID,
    epoch: str,
    freshness: changes.Freshness | None = None,
    *,
    profile: bool = False,
) -> changes.Feed:
    """
    Feed einer Kommune mit den Adressen nach den Profilen von kommParl (``profile``) oder der älteren Form, dazu die
    Einbettungen des Bestands (``bestand.EMBEDDINGS``, nur nach dem Profil genutzt).
    """
    uris = output.uris

    def addresses(kind: str, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
        # Die Kennung im Ereignis ist die des Bestands, und der Bestand enthält nur Öffentliches. Gibt es
        # das Objekt nicht (noch nicht übernommen oder fälschlich als öffentlich gemeldet), gibt es keinen
        # Eintrag – wie bei der Session-Schnittstelle
        return {object_id: uris.obj(kind, object_id) for object_id in _existing(kind, ids)}

    def current(kind: str, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
        return {object_id: uris.obj(kind, object_id) for object_id in _current(kind, ids)}

    return changes.Feed(
        body_id=pk,
        url=uris.kommparl_changes(pk) if profile else uris.changes(pk),
        snapshot_url=uris.kommparl_snapshot(pk) if profile else uris.snapshot(pk),
        addresses=addresses,
        epoch=epoch,
        freshness=freshness,
        embeds=bestand.EMBEDDINGS,
        related=_related(pk),
        current=current,
    )


def _feed_or_unavailable(
    request: HttpRequest, output: BestandMapping, pk: uuid.UUID, segment: str, *, profile: bool = False
) -> changes.Feed | HttpResponse:
    """
    Feed einer Kommune – oder die Antwort, warum es für sie weder Feed noch Snapshot gibt.

    Feed und Snapshot gibt es nur für Kommunen, die das Bürgerportal veröffentlicht und listet. Es gelten
    dieselben Stände wie für seine Seiten (``insight_core.publication``):

    - vorübergehend abgeschaltet: ``503`` mit ``Retry-After``. Der Cursor bleibt gültig – es wird nichts
      gelöscht, und was in der Zwischenzeit geschieht, steht im Journal.
    - dauerhaft zurückgenommen: ``410`` (``changes.withdrawn_response``). Die Rücknahme und eine spätere
      Wiederherstellung ändern den Bestand am Journal vorbei; danach gilt ein älterer Cursor nicht mehr
      (``Feed.epoch`` aus ``publication.retracted_at``), der Abnehmer steigt über den Snapshot neu ein.
    - nicht gelistet (etwa die Demo-Kommune oder eine Kommune im Aufbau): Die Adressen gibt es nicht, wie
      bei ausgeschaltetem Feed; der Body nennt sie nicht.
    - in der Quelle gelöscht: Der Snapshot nach dem Profil ist ``410`` (``changes.kommparl_deleted_response``),
      denn er enthält keine gelöschten Objekte, auch nicht den Body. Der Feed bleibt und nennt die Löschung.

    Alle Absagen sind feste Antworten: Sie hängen nicht davon ab, was im Journal steht. Nach den Profilen von
    kommParl (``profile``) ist die Rücknahme ein Fehlerobjekt nach OParl 1.1, sonst ein Problem nach RFC 9457.
    """
    if not changes.enabled():
        # Ausgeschaltet gibt es die Adresse nicht
        return _unknown_list(segment)
    paused = _paused_response(pk)
    if paused is not None:
        return paused
    state = publication.body_state(pk)
    if state is not None and state.withdrawn:
        return changes.kommparl_withdrawn_response() if profile else changes.withdrawn_response(request)
    found = (
        OParlBody.objects.filter(pk=pk)
        .values_list(
            "is_listed",
            "deleted",
            "source__sync_config",
            "source_id",
            "source__last_successful_sync",
            "source__last_successful_full_sync",
        )
        .first()
    )
    if found is None:
        return error_response(404, "Kommune (Body) nicht gefunden.")
    listed, deleted, config, source_id, synced_at, full_synced_at = found
    if not listed:
        return _unknown_list(segment)
    if deleted and profile and segment == "snapshot":
        # Profil Snapshot: „Gelöschte Objekte sind nicht enthalten“ – ohne Body gäbe es keinen gültigen Snapshot
        return changes.kommparl_deleted_response()
    # Aktualität der Quelle (Issue #556): Der Feed ist nur so aktuell wie ihr letzter vollständiger Abgleich
    freshness = None if source_id is None else changes.Freshness(synced_at=synced_at, full_synced_at=full_synced_at)
    return _feed(output, pk, publication.retracted_at(config), freshness, profile=profile)


def _deprecated(response: HttpResponse, successor: str) -> HttpResponse:
    """Ältere Form von Feed und Snapshot: abgekündigt (``changes.deprecated``), außer wo es die Adresse nicht gibt."""
    if response.status_code != 404:
        changes.deprecated(response, successor)
    return response


@endpoint
def body_changes(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Änderungsfeed einer Kommune in der älteren Form (abgekündigt, Nachfolger ``body_kommparl_changes``)."""
    output = mapping()
    feed = _feed_or_unavailable(request, output, pk, "changes")
    response = feed if isinstance(feed, HttpResponse) else changes.changes_response(request, feed)
    return _deprecated(response, output.uris.kommparl_changes(pk))


@endpoint
def body_kommparl_changes(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    """Änderungsfeed einer Kommune nach dem Profil von kommParl (``kommparl:changes``, ``hub.api.changes``)."""
    feed = _feed_or_unavailable(request, mapping(), pk, "changes", profile=True)
    if isinstance(feed, HttpResponse):
        return feed
    return changes.kommparl_changes_response(request, feed)


def _section(output: BestandMapping, spec: Spec, queryset: QuerySet[Any]) -> snapshot.Section:
    def render(objects: list[Any]) -> list[Objekt]:
        ctx = spec.context(objects)
        return [spec.render(output, obj, ctx) for obj in objects]

    return snapshot.Section(queryset=queryset, render=render)


def _body(output: BestandMapping, pk: uuid.UUID) -> Callable[[], Objekt]:
    def body() -> Objekt:
        obj = _BODY.queryset(pk=pk).get()
        # Eine zurückgenommene Kommune steht auch im Snapshot nur als gekürztes Objekt
        return output.tombstone("body", obj) if obj.deleted else output.body(obj)

    return body


def _snapshot(output: BestandMapping, pk: uuid.UUID, feed: changes.Feed) -> snapshot.Snapshot:
    """Snapshot in der älteren Form: der Body und die Objekte der externen Listen mit ihren Einbettungen."""
    sections = [_section(output, spec, spec.queryset(body_id=pk, deleted=False)) for spec in BODY_LISTS.values()]
    return snapshot.Snapshot(feed, _body(output, pk), sections)


#: Dateien im Snapshot nach kommParl: wie in den Listen ohne den erkannten Text. Dateiinhalte gehören nach dem Profil
#: nicht in den Snapshot; der Abnehmer ruft sie über ``accessUrl`` ab, den Text am Objekt-Endpunkt.
_SNAPSHOT_FILE = Spec("file", OParlFile, BestandMapping.file, lambda queryset: queryset.defer("text_content"))


def _meeting_locations(output: BestandMapping, pk: uuid.UUID) -> snapshot.Section:
    """
    Sitzungsorte unter der Kennung ihrer Sitzung (``…/location/<Kennung der Sitzung>``): jeder, den der Objekt-Endpunkt
    ausliefert (``_meeting_location_response``), also jede Sitzung mit Ortsangabe – auch wenn sie daneben ein eigenes
    Location-Objekt der Quelle einbettet. Das Profil Snapshot verlangt jedes öffentliche Objekt (Issue #1229).
    """

    def render(meetings: list[Any]) -> list[Objekt]:
        return [data for meeting in meetings if (data := output.meeting_location(meeting)) is not None]

    return snapshot.Section(queryset=OParlMeeting.objects.filter(body_id=pk, deleted=False), render=render)


def _kommparl_snapshot(output: BestandMapping, pk: uuid.UUID, feed: changes.Feed) -> snapshot.Snapshot:
    """
    Snapshot nach dem Profil von kommParl: der Body und jedes Objekt der Kommune in einer eigenen Zeile, wie unter
    seiner Adresse (eingebettete Objekte stehen zusätzlich in ihren Trägern), ohne Gelöschtes.
    """

    def section(spec: Spec, condition: Q) -> snapshot.Section:
        queryset = spec.model._default_manager.filter(condition, deleted=False)
        return _section(output, spec, spec.prepare(queryset) if spec.prepare else queryset)

    own = Q(body_id=pk)
    sections = [
        section(OBJECT_TYPES["legislativeterm"], own),
        section(_ORGANIZATION, own),
        section(_PERSON, own),
        section(OBJECT_TYPES["membership"], Q(person__body_id=pk) | Q(organization__body_id=pk)),
        section(_MEETING, own),
        section(OBJECT_TYPES["agendaitem"], Q(meeting__body_id=pk)),
        section(_PAPER, own),
        section(OBJECT_TYPES["consultation"], own),
        section(_SNAPSHOT_FILE, own),
        section(_LOCATION, own),
        _meeting_locations(output, pk),
    ]
    return snapshot.Snapshot(feed, _body(output, pk), sections)


def snapshot_for_measurement(pk: uuid.UUID) -> snapshot.Snapshot:
    """
    Snapshot einer Kommune wie unter ``…/snapshot``, ohne Schalter und Veröffentlichungsstand zu prüfen.

    Nur für die Messung von Bauzeit und Größe vor dem Einschalten (``manage.py oparl_snapshot_messen``); die
    Ausgabe geht an niemanden. Abnehmer erreichen den Snapshot ausschließlich über ``body_snapshot``.
    """
    output = mapping()
    found = OParlBody.objects.filter(pk=pk).values_list("source__sync_config", flat=True).first()
    return _kommparl_snapshot(output, pk, _feed(output, pk, publication.retracted_at(found), profile=True))


@endpoint
def body_snapshot(request: HttpRequest, pk: uuid.UUID) -> HttpResponseBase:
    """Snapshot einer Kommune in der älteren Form (abgekündigt): dieselben Objekte wie die Listen."""
    output = mapping()
    successor = output.uris.kommparl_snapshot(pk)
    feed = _feed_or_unavailable(request, output, pk, "snapshot")
    if isinstance(feed, HttpResponse):
        return _deprecated(feed, successor)
    return changes.deprecated(snapshot.snapshot_response(request, _snapshot(output, pk, feed)), successor)


@endpoint
def body_kommparl_snapshot(request: HttpRequest, pk: uuid.UUID) -> HttpResponseBase:
    """Snapshot einer Kommune nach dem Profil von kommParl (``kommparl:snapshot``, ``hub.api.snapshot``)."""
    output = mapping()
    feed = _feed_or_unavailable(request, output, pk, "snapshot", profile=True)
    if isinstance(feed, HttpResponse):
        return feed
    return snapshot.kommparl_snapshot_response(request, _kommparl_snapshot(output, pk, feed))


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
