# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung der kanonischen Kennungen im RIS-Bestand (nur lesend).

Kanonisch ist ``id == uuid5(NS_MANDARI_RIS, external_id)`` (``shared/mandari_oparl/ids.py``, ADR
``docs/adr/20260929-kanonisches-modell.md``). Neue Objekte bekommen diese Kennung im Ingestor und in
Django; ältere Objekte aus Django tragen noch zufällige Kennungen. Die Prüfung zählt je Quelle und
Entität:

- **Kennung abweichend:** ``id`` ist nicht die kanonische Kennung der ``external_id``. Hat die Quelle eine
  festgeschriebene Basis der Kennungen (``sync_config["id_base"]``, nach einem Umzug, ggf. mit
  Abbildungsregeln ``id_rules``), gilt die Kennung der Adresse auf dieser Basis (``OParlSource.id_bases``,
  wie Ingestor und Spiegel). Objekte ohne zuordenbare Quelle prüft sie mit den Basen aller Quellen, wie
  der Ingestor sie über alle Quellen hinweg anwendet.
- **URI abweichend** (nur Quellen aus mandari Session): ``external_id`` liegt nicht unter der
  öffentlichen OParl-Adresse der Installation (``SITE_URL``), sondern z. B. unter einem anderen Host.
- **Ohne URI:** leere ``external_id``; eine kanonische Kennung ist nicht bestimmbar.
- **Kollision:** Die kanonische Kennung eines abweichenden Objekts ist bereits an ein anderes Objekt
  derselben Tabelle vergeben; eine Umschlüsselung wäre dort nicht ohne Weiteres möglich.

Daneben vergleicht die Prüfung die festgeschriebene Basis der Kennungen der Installation
(``apps.common.identifiers``, Issue #733) mit ``SITE_URL`` und je Session-Quelle die Basis ihrer Kennungen
mit der erwarteten.

Die Prüfung schreibt nichts. Sie liest den Bestand seitenweise nach Primärschlüssel, damit auch große
Tabellen mit wenig Speicher auskommen.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db.models import F, Model, QuerySet
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse
from mandari_oparl.ids import IdBases, source_id_base

from apps.common.identifiers import site_url, stored_identifier_base
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
    OParlSource,
)

#: Seitengröße beim Lesen des Bestands
BATCH_SIZE = 5000


@dataclass(frozen=True)
class Entity:
    """Eine Entität des RIS-Bestands und der Weg zu ihrer Körperschaft (für die Zuordnung zur Quelle)."""

    name: str
    model: type[Model]
    body_paths: tuple[str, ...]


#: Alle Entitäten des kanonischen Modells in fachlicher Reihenfolge
ENTITIES: tuple[Entity, ...] = (
    Entity("body", OParlBody, ()),
    Entity("legislativeterm", OParlLegislativeTerm, ("body_id",)),
    Entity("organization", OParlOrganization, ("body_id",)),
    Entity("person", OParlPerson, ("body_id",)),
    Entity("membership", OParlMembership, ("person__body_id", "organization__body_id")),
    Entity("meeting", OParlMeeting, ("body_id",)),
    Entity("agendaitem", OParlAgendaItem, ("meeting__body_id",)),
    Entity("paper", OParlPaper, ("body_id",)),
    Entity("consultation", OParlConsultation, ("body_id", "paper__body_id")),
    Entity("file", OParlFile, ("body_id", "paper__body_id", "meeting__body_id")),
    Entity("location", OParlLocation, ("body_id",)),
)


@dataclass
class Count:
    """Zählung je Quelle und Entität."""

    objects: int = 0
    id_deviations: int = 0
    uri_deviations: int = 0
    without_uri: int = 0
    collisions: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, other: Count) -> None:
        self.objects += other.objects
        self.id_deviations += other.id_deviations
        self.uri_deviations += other.uri_deviations
        self.without_uri += other.without_uri
        self.collisions += other.collisions


@dataclass(frozen=True)
class SourceInfo:
    """Quelle im Bericht; ``session_base`` und ``expected_id_base`` nur für Quellen aus mandari Session."""

    key: str
    name: str
    url: str
    session_tenant: str | None = None
    #: Erwartete Adresse: Schnittstelle des Mandanten auf ``SITE_URL``
    session_base: str | None = None
    #: Basis der Kennungen der Quelle: ``sync_config["id_base"]``, ohne Eintrag ihre Adresse
    id_base: str = ""
    #: Erwartete Basis der Kennungen: Schnittstelle des Mandanten auf der festgeschriebenen Basis
    expected_id_base: str | None = None
    #: Kanonische Kennungen der Objekte dieser Quelle
    ids: IdBases = field(default_factory=IdBases, compare=False)

    @property
    def id_base_deviates(self) -> bool:
        """Die Quelle bildet ihre Kennungen auf einer anderen Basis als die Session-Schnittstelle."""
        return self.expected_id_base is not None and self.id_base != self.expected_id_base


@dataclass
class Report:
    """Ergebnis der Prüfung: Zählungen je Quelle und Entität."""

    sources: dict[str, SourceInfo] = field(default_factory=dict)
    counts: dict[tuple[str, str], Count] = field(default_factory=dict)
    #: Festgeschriebene Basis der Kennungen (``None``: noch keine) und aktuelle öffentliche Adresse
    identifier_base: str | None = None
    site_url: str = ""
    #: Kanonische Kennungen über alle geprüften Quellen (für Objekte ohne zuordenbare Quelle)
    ids: IdBases = field(default_factory=IdBases, compare=False)

    @property
    def base_deviates(self) -> bool:
        """Die festgeschriebene Basis der Kennungen ist nicht ``SITE_URL`` (gewollt nur nach einem Domainwechsel)."""
        return self.identifier_base is not None and self.identifier_base != self.site_url

    def count(self, source_key: str, entity: str) -> Count:
        return self.counts.setdefault((source_key, entity), Count())

    def totals_by_entity(self) -> dict[str, Count]:
        totals: dict[str, Count] = {}
        for (_source, entity), count in self.counts.items():
            totals.setdefault(entity, Count()).add(count)
        return totals

    def total(self) -> Count:
        total = Count()
        for count in self.counts.values():
            total.add(count)
        return total


#: Schlüssel für Objekte ohne zuordenbare Körperschaft
NO_SOURCE = "-"


class InvalidIdRulesError(ValueError):
    """Die Abbildungsregeln einer Quelle (``sync_config["id_rules"]``) sind ungültig; die Meldung nennt die Quelle."""


def session_oparl_base(tenant_slug: str, base: str | None = None) -> str | None:
    """
    OParl-Adresse eines Session-Mandanten auf Basis von ``SITE_URL`` bzw. ``base`` (endet mit ``/``).

    Entspricht ``apps.session.services.insight_service.oparl_system_url`` (mit ``base``:
    ``oparl_id_base``) ohne dessen Import: Der RIS-Bestand hängt nicht von einem Fachmodul ab (ADR
    20260929-schichtenmodell).
    """
    try:
        path = reverse("session:oparl_system", kwargs={"tenant_slug": tenant_slug})
    except NoReverseMatch:
        return None
    return f"{(base or site_url()).rstrip('/')}{path}"


def _source_info(source: OParlSource, identifier_base: str) -> SourceInfo:
    config = source.sync_config if isinstance(source.sync_config, dict) else {}
    tenant = str(config.get("session_tenant") or "") or None
    return SourceInfo(
        key=str(source.pk),
        name=source.name,
        url=source.url,
        session_tenant=tenant,
        session_base=session_oparl_base(tenant) if tenant else None,
        id_base=source_id_base(config) or source.url,
        expected_id_base=session_oparl_base(tenant, identifier_base) if tenant else None,
        ids=source.id_bases(),
    )


def _pages(queryset: QuerySet[Any, Any], fields: list[str]) -> Iterator[list[dict[str, Any]]]:
    """Den Bestand seitenweise nach Primärschlüssel lesen (ohne langen Datenbank-Cursor)."""
    last: UUID | None = None
    while True:
        page_qs = queryset.order_by("pk")
        if last is not None:
            page_qs = page_qs.filter(pk__gt=last)
        page = list(page_qs.values(*fields)[:BATCH_SIZE])
        if not page:
            return
        yield page
        last = page[-1]["pk"]


def _check_entity(
    entity: Entity,
    report: Report,
    body_sources: dict[UUID, str],
    only_source: str | None,
    examples: int,
) -> None:
    queryset: QuerySet[Any, Any] = entity.model._default_manager.all()
    if entity.body_paths:
        paths = entity.body_paths
        queryset = queryset.annotate(ris_body=Coalesce(*paths) if len(paths) > 1 else F(paths[0]))
        if only_source:
            queryset = queryset.filter(ris_body__in=[b for b, s in body_sources.items() if s == only_source])
        fields = ["pk", "external_id", "ris_body"]
    else:
        if only_source:
            queryset = queryset.filter(source_id=only_source)
        fields = ["pk", "external_id", "source_id"]

    for page in _pages(queryset, fields):
        deviating: dict[UUID, str] = {}
        for row in page:
            if entity.body_paths:
                source_key = body_sources.get(row["ris_body"], NO_SOURCE) if row["ris_body"] else NO_SOURCE
            else:
                source_key = str(row["source_id"]) if row["source_id"] else NO_SOURCE
            count = report.count(source_key, entity.name)
            count.objects += 1
            external_id = row["external_id"] or ""
            if not external_id:
                count.without_uri += 1
                continue
            info = report.sources.get(source_key)
            if info is not None and info.session_base and not external_id.startswith(info.session_base):
                count.uri_deviations += 1
                if len(count.examples) < examples:
                    count.examples.append(f"URI: {external_id}")
            expected = (info.ids if info is not None else report.ids).id(external_id)
            if row["pk"] != expected:
                count.id_deviations += 1
                deviating[expected] = source_key
                if len(count.examples) < examples:
                    count.examples.append(f"Kennung {row['pk']} statt {expected}: {external_id}")
        if deviating:
            taken = entity.model._default_manager.filter(pk__in=list(deviating)).values_list("pk", flat=True)
            for pk in taken:
                report.count(deviating[pk], entity.name).collisions += 1


def check_ris_ids(only_source: str | None = None, examples: int = 0) -> Report:
    """
    Kennungen und URIs des RIS-Bestands prüfen; ``only_source`` = Primärschlüssel oder URL einer Quelle.

    Liest nur. Liefert einen Bericht je Quelle und Entität.
    """
    report = Report(identifier_base=stored_identifier_base(), site_url=site_url())
    # Ohne festgeschriebene Basis gilt beim ersten Bedarf SITE_URL (apps.common.identifiers)
    identifier_base = report.identifier_base or report.site_url
    sources = OParlSource.objects.all()
    if only_source and "://" in only_source:
        sources = sources.filter(url=only_source)
    elif only_source:
        try:
            sources = sources.filter(pk=UUID(only_source))
        except ValueError:
            raise LookupError("Quelle nicht gefunden.") from None
    for source in sources:
        try:
            report.sources[str(source.pk)] = _source_info(source, identifier_base)
            report.ids.add_source(source.url, source.sync_config)
        except ValueError as exc:
            raise InvalidIdRulesError(f"Quelle {source.pk}: {exc}") from None
    if only_source and not report.sources:
        raise LookupError("Quelle nicht gefunden.")
    selected = next(iter(report.sources)) if only_source else None

    body_sources = {
        body: str(source)
        for body, source in OParlBody.objects.filter(source__isnull=False).values_list("pk", "source_id")
    }
    for entity in ENTITIES:
        _check_entity(entity, report, body_sources, selected, examples)
    return report
