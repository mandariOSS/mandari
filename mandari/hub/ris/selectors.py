# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lese-Fassade des RIS-Bestands (``docs/adr/20260929-kanonisches-modell.md``, Issue #522).

Fachmodule (Work, Session, Bürgerportal) lesen RIS-Daten über diese fachlichen Abfragen statt direkt
über ``OParl*.objects``. So hängen sie nicht am Tabellenschema von ``insight_core``, und Regeln wie
„zurückgenommene Beratungen zählen nicht“ stehen an einer Stelle.

- Abfragen über „die Kommunen“ nehmen ``bodies`` entgegen: ein QuerySet oder eine Liste von
  ``OParlBody`` (bzw. deren Kennungen), in der Regel ``organization.get_all_bodies()``.
- Einzelabfragen (``meeting``, ``paper`` …) nehmen die Kennung, wie sie aus einer URL oder einem
  Formular kommt; eine ungültige Kennung ergibt ``None`` bzw. ``False`` statt eines Fehlers.
- Rückgaben sind vorerst QuerySets der Bestandsmodelle, damit Aufrufer weiter vorladen
  (``prefetch_related``), begrenzen und zählen können. Filter auf Tabellenfelder gehören in neue
  Funktionen hier, nicht in den Aufrufer.

Die Fassade liest nur. Geschrieben wird der RIS-Bestand vom Ingestor und von der Abbildung für
Session-Mandanten. ``scripts/check_ris_access_ratchet.py`` zählt Direktzugriffe außerhalb von ``hub``
und ``insight_core``; die Zahl darf nur sinken.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, cast

from django.db.models import Exists, F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce, Length, Substr
from django.utils import timezone
from mandari_dokumente import MAX_TEXT_CHARS

from hub.ris import vorlagennummer
from hub.ris.canonical import implementation_extension, roll_call_extension, vote_extension
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    withdrawn_q,
)

#: Kommunen, auf die sich eine Abfrage beschränkt.
Bodies = QuerySet[OParlBody] | Iterable[OParlBody | uuid.UUID]
#: Gremien als Objekte oder Kennungen.
Organizations = Iterable[OParlOrganization | uuid.UUID]


def _uuid(value: object) -> uuid.UUID | None:
    """Kennung aus URL oder Formular; ``None``, wenn es keine gültige UUID ist."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# =============================================================================
# Quellen
# =============================================================================


def active_source(source_id: object) -> OParlSource | None:
    """Aktive RIS-Quelle (Sync-Auslöser im Admin, Issue #1026); sonst ``None``."""
    pk = _uuid(source_id)
    return OParlSource.objects.filter(pk=pk, is_active=True).first() if pk else None


# =============================================================================
# Beratungen: Bezüge nur innerhalb einer Quelle
# =============================================================================
#
# Eine Beratung nennt ihre Sitzung und ihren Tagesordnungspunkt nur als Kennung der Quelle (Text), nicht als
# Fremdschlüssel. Ein solcher Bezug gilt nur, wenn Sitzung bzw. Punkt zur selben Quelle gehören wie die Kommune der
# Beratung (``body__source_id``); Beratungen ohne Kommune (Altbestand) schränkt das nicht ein. Jede Abfrage, die
# Beratungen über diese Kennungen mit Sitzungen oder Punkten verbindet, nimmt eine der Bedingungen hier.


def meeting_in_consultation_source() -> Q:
    """
    Bedingung auf ``OParlMeeting`` in einer Unterabfrage je Beratung (``OuterRef`` auf ``OParlConsultation``): Die
    Sitzung gehört zur Quelle der Kommune der Beratung; bei einer Beratung ohne Kommune gilt jede Sitzung.
    """
    return Q(body__source_id=Coalesce(OuterRef("body__source_id"), F("body__source_id")))


def agenda_item_in_consultation_source() -> Q:
    """Wie ``meeting_in_consultation_source`` für ``OParlAgendaItem`` (Quelle über die Sitzung des Punkts)."""
    return Q(meeting__body__source_id=Coalesce(OuterRef("body__source_id"), F("meeting__body__source_id")))


def consultation_meeting_in_source() -> Exists:
    """
    Bedingung auf ``OParlConsultation``: Die Sitzung aus ``meeting_external_id`` steht im Bestand und gehört zur
    Quelle der Beratung (``meeting_in_consultation_source``). Eine Abfrage, je Beratung ein Indexzugriff.
    """
    return Exists(
        OParlMeeting.objects.filter(meeting_in_consultation_source(), external_id=OuterRef("meeting_external_id"))
    )


def consultation_agenda_item_in_source() -> Exists:
    """Wie ``consultation_meeting_in_source`` für den Tagesordnungspunkt aus ``agenda_item_external_id``."""
    return Exists(
        OParlAgendaItem.objects.filter(
            agenda_item_in_consultation_source(), external_id=OuterRef("agenda_item_external_id")
        )
    )


def with_reference_checks(consultations: QuerySet[OParlConsultation]) -> QuerySet[OParlConsultation]:
    """
    Beratungen mit ``meeting_in_source`` und ``agenda_item_in_source`` (bool): Sitzung bzw. Tagesordnungspunkt
    gehören zur Quelle der Beratung. Für Aufrufer, die die Bezüge selbst über ihre Kennung nachschlagen: Ist das Feld
    falsch, gilt der Bezug als unbekannt.
    """
    return consultations.annotate(
        meeting_in_source=consultation_meeting_in_source(),
        agenda_item_in_source=consultation_agenda_item_in_source(),
    )


def consultation_references_in_source(consultation_ids: Iterable[object]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """
    Kennungen der Beratungen, deren Sitzung bzw. Tagesordnungspunkt zu ihrer Quelle gehört (``with_reference_checks``),
    für schon geladene Beratungen. Eine Abfrage.
    """
    ids = [pk for pk in (_uuid(value) for value in consultation_ids) if pk is not None]
    sitzungen: set[uuid.UUID] = set()
    punkte: set[uuid.UUID] = set()
    if not ids:
        return sitzungen, punkte
    rows = with_reference_checks(OParlConsultation.objects.filter(pk__in=ids)).values_list(  # type: ignore[misc]
        "pk", "meeting_in_source", "agenda_item_in_source"
    )
    for pk, sitzung, punkt in rows:
        if sitzung:
            sitzungen.add(pk)
        if punkt:
            punkte.add(pk)
    return sitzungen, punkte


# =============================================================================
# Sitzungen
# =============================================================================


def body_ids_by_slug(slugs: Iterable[str]) -> dict[str, str]:
    """Kennungen von Kommunen zu ihren Kurznamen (``muenster`` → UUID als Text); unbekannte fehlen."""
    rows = OParlBody.objects.filter(slug__in=list(slugs)).values_list("slug", "id")
    return {str(slug): str(pk) for slug, pk in rows}


def meetings(bodies: Bodies) -> QuerySet[OParlMeeting]:
    """Sitzungen der Kommunen."""
    return OParlMeeting.objects.filter(body__in=bodies)


def meeting(bodies: Bodies, meeting_id: object) -> OParlMeeting | None:
    """Eine Sitzung der Kommunen; ``None``, wenn es sie dort nicht gibt."""
    pk = _uuid(meeting_id)
    return meetings(bodies).filter(pk=pk).first() if pk else None


def meeting_by_id(meeting_id: object) -> OParlMeeting | None:
    """
    Eine Sitzung über alle Kommunen – für Ausgaben, die nicht auf Kommunen beschränkt sind (der
    OParl-Aggregator). Auch eine in der Quelle gelöschte oder zurückgenommene Sitzung: Der Aufrufer
    entscheidet, was er dafür ausgibt. ``None``, wenn es sie nicht gibt.
    """
    pk = _uuid(meeting_id)
    return OParlMeeting.objects.filter(pk=pk).first() if pk else None


def upcoming_meetings(bodies: Bodies, *, since: datetime | None = None) -> QuerySet[OParlMeeting]:
    """Nicht abgesagte Sitzungen ab ``since`` (Standard: jetzt), nach Beginn sortiert."""
    since = since or timezone.now()
    return meetings(bodies).filter(start__gte=since, cancelled=False).order_by("start")


def search_meetings(bodies: Bodies, text: str = "", *, on: date | None = None) -> QuerySet[OParlMeeting]:
    """Sitzungen an einem Tag (``on``) oder mit ``text`` im Namen, neueste zuerst."""
    found = meetings(bodies)
    found = found.filter(start__date=on) if on else found.filter(name__icontains=text)
    return found.order_by("-start")


def meetings_of_organizations(
    organizations: Organizations,
    *,
    starts_from: datetime | None = None,
    starts_after: datetime | None = None,
    starts_until: datetime | None = None,
    include_cancelled: bool = True,
) -> QuerySet[OParlMeeting]:
    """
    Sitzungen, an denen mindestens eines der Gremien beteiligt ist, nach Beginn sortiert.

    Der Zeitraum ist wahlweise ab ``starts_from`` (einschließlich) oder nach ``starts_after``
    (ausschließlich) und bis ``starts_until`` (einschließlich); mit einer Grenze fallen Sitzungen
    ohne Beginn heraus. Jede Sitzung erscheint einmal, auch wenn mehrere Gremien sie abhalten.
    """
    found = OParlMeeting.objects.filter(organizations__in=list(organizations))
    if starts_from is not None:
        found = found.filter(start__gte=starts_from)
    if starts_after is not None:
        found = found.filter(start__gt=starts_after)
    if starts_until is not None:
        found = found.filter(start__lte=starts_until)
    if not include_cancelled:
        found = found.filter(cancelled=False)
    return found.distinct().order_by("start")


def meetings_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlMeeting]:
    """Sitzungen zu OParl-Kennungen (URLs), etwa aus einer Beratung."""
    return OParlMeeting.objects.filter(external_id__in=list(external_ids))


# =============================================================================
# Tagesordnungspunkte
# =============================================================================


def agenda_items(meeting: OParlMeeting) -> QuerySet[OParlAgendaItem]:
    """Tagesordnungspunkte einer Sitzung in ihrer Reihenfolge."""
    return OParlAgendaItem.objects.filter(meeting=meeting).order_by("order", "number")


def agenda_number_key(number: str | None) -> list[tuple[int, int | str]]:
    """Natürliche Sortierung von TOP-Nummern: 1, 2, 10 statt 1, 10, 2; Punkte ohne Nummer stehen hinten (wie „999“)."""
    return [(0, int(p)) if p.isdecimal() else (1, p.lower()) for p in re.split(r"(\d+)", number or "999") if p]


def visible_agenda_items(meeting: OParlMeeting) -> list[OParlAgendaItem]:
    """
    Tagesordnung, wie die Sitzungsseiten von Insight und Work sie zeigen (Issue #853): ohne in der Quelle gelöschte und
    von mandari Session zurückgenommene Punkte (etwa nachträglich nichtöffentlich gestellt), natürlich sortiert
    (``agenda_number_key``, bei gleicher Nummer in der Reihenfolge der Quelle). Eine Abfrage.

    Die Vorbereitung in Work zeigt weiter alle Punkte samt Positionen und Notizen der Organisation.
    """
    punkte = list(OParlAgendaItem.objects.filter(meeting=meeting, deleted=False).order_by("order", "number"))
    punkte.sort(key=lambda punkt: agenda_number_key(punkt.number))
    return punkte


def agenda_item_exists(agenda_item_id: object) -> bool:
    """Gibt es diesen Tagesordnungspunkt im RIS-Bestand?"""
    pk = _uuid(agenda_item_id)
    return pk is not None and OParlAgendaItem.objects.filter(pk=pk).exists()


@dataclass(frozen=True)
class Decision:
    """
    Beschlussfassung an einem Tagesordnungspunkt (kanonisches Modell, Issue #525), in der Form der Erweiterungen
    ``mandari:*`` der offenen Schnittstelle: Beschlussnummer, Abstimmung (Art, Ergebnis, Summen, je mit Bezeichnung),
    Einzelstimmen nur bei namentlicher Abstimmung, veröffentlichter Umsetzungsstand. Fehlendes ist ``None``.
    """

    resolution_number: str | None
    vote: dict[str, Any] | None
    roll_call: list[dict[str, Any]] | None
    implementation: dict[str, Any] | None


def decision(agenda_item: OParlAgendaItem) -> Decision:
    """Beschlussfassung eines Tagesordnungspunkts aus dem RIS-Bestand (ohne Abfrage)."""
    return Decision(
        resolution_number=agenda_item.resolution_number or None,
        vote=vote_extension(agenda_item),
        roll_call=roll_call_extension(agenda_item),
        implementation=implementation_extension(agenda_item),
    )


@dataclass(frozen=True)
class ProtocolApproval:
    """Genehmigung der veröffentlichten Niederschrift einer Sitzung (Issue #525)."""

    #: ``follow_up`` (in der Folgesitzung) oder ``direct`` (ohne Genehmigungsschritt veröffentlicht)
    mode: str
    approved_on: date | None
    #: genehmigende Sitzung, sofern sie im Bestand ist
    approved_in: OParlMeeting | None


def protocol_approval(meeting: OParlMeeting) -> ProtocolApproval | None:
    """Genehmigung der Niederschrift; ``None`` ohne Angabe. Höchstens eine Abfrage (genehmigende Sitzung)."""
    if not meeting.protocol_approval_mode:
        return None
    approved_in = None
    if meeting.protocol_approved_in_external_id:
        approved_in = OParlMeeting.objects.filter(
            external_id=meeting.protocol_approved_in_external_id, deleted=False
        ).first()
    return ProtocolApproval(
        mode=meeting.protocol_approval_mode, approved_on=meeting.protocol_approved_on, approved_in=approved_in
    )


def agenda_items_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlAgendaItem]:
    """Tagesordnungspunkte zu OParl-Kennungen (URLs), etwa aus einer Beratung."""
    return OParlAgendaItem.objects.filter(external_id__in=list(external_ids))


def papers_of_agenda_item(agenda_item: OParlAgendaItem) -> QuerySet[OParlPaper]:
    """Vorlagen, die unter einem Tagesordnungspunkt beraten werden (ohne zurückgenommene Beratungen)."""
    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id=agenda_item.external_id, paper__isnull=False)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
    )
    return OParlPaper.objects.filter(id__in=consultations.values("paper_id")).distinct()


def paper_ids_of_agenda_items(agenda_item_ids: Iterable[object], *, public_only: bool = True) -> set[uuid.UUID]:
    """
    Kennungen der Vorlagen, die unter diesen Tagesordnungspunkten beraten werden (ohne zurückgenommene Beratungen
    und Vorlagen). Eine Abfrage, etwa für den Bezug einer Fraktion in der Suche (Issue #853).

    ``public_only`` (Standard): nur öffentliche Tagesordnungspunkte. Was jemand zu einem nichtöffentlichen Punkt
    vermerkt hat, soll sich nicht in der Reihenfolge öffentlicher Vorlagen zeigen (wie bei Fraktionssitzungen).
    """
    ids = [pk for pk in (_uuid(value) for value in agenda_item_ids) if pk is not None]
    if not ids:
        return set()
    items = OParlAgendaItem.objects.filter(id__in=ids)
    if public_only:
        items = items.filter(public=True)
    return set(
        OParlConsultation.objects.filter(agenda_item_external_id__in=items.values("external_id"), paper__isnull=False)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
        .values_list("paper_id", flat=True)
    )


def paper_ids_on_upcoming_agendas(bodies: Bodies, *, until: datetime, limit: int = 2000) -> set[uuid.UUID]:
    """
    Kennungen der Vorlagen, die von jetzt bis ``until`` in einer nicht abgesagten Sitzung der Kommunen beraten werden
    (ohne zurückgenommene Beratungen und Vorlagen). Eine Abfrage, etwa für die Aktualität in der Suche (Issue #853).
    """
    sitzungen = upcoming_meetings(bodies).filter(start__lte=until).values("external_id")
    return set(
        OParlConsultation.objects.filter(meeting_external_id__in=sitzungen, paper__isnull=False)
        .filter(consultation_meeting_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
        .values_list("paper_id", flat=True)[:limit]
    )


def organization_names(organization_ids: Iterable[object], *, exclude_classifications: Iterable[str] = ()) -> list[str]:
    """
    Namen der Gremien zu diesen Kennungen (ohne leere Namen), alphabetisch. ``exclude_classifications`` lässt Gremien
    mit dieser Einordnung aus (ohne Rücksicht auf Groß- und Kleinschreibung), etwa die Vertretung selbst („Rat“).
    """
    ids = [pk for pk in (_uuid(value) for value in organization_ids) if pk is not None]
    if not ids:
        return []
    ohne = {str(value).strip().lower() for value in exclude_classifications}
    rows = OParlOrganization.objects.filter(id__in=ids).exclude(name="").values_list("name", "classification")
    return sorted(
        {name for name, classification in rows if name and (classification or "").strip().lower() not in ohne}
    )


@dataclass(frozen=True)
class AgendaOverview:
    """Umfang einer Tagesordnung: Zahl der Punkte und die Punkte, unter denen eine Vorlage beraten wird."""

    items: int
    with_paper: frozenset[uuid.UUID]


def agenda_overview(meeting_ids: Iterable[object]) -> dict[uuid.UUID, AgendaOverview]:
    """
    Je Sitzung die Zahl der Tagesordnungspunkte und die Punkte mit Vorlage (ohne zurückgenommene Beratungen),
    für den Vorbereitungsstand („2 von 9 Vorlagen“). Eine Abfrage für alle Sitzungen; Sitzungen ohne Punkte fehlen.
    """
    ids = [pk for pk in (_uuid(value) for value in meeting_ids) if pk is not None]
    if not ids:
        return {}
    mit_vorlage = (
        OParlConsultation.objects.filter(agenda_item_external_id=OuterRef("external_id"), paper__isnull=False)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
    )
    zeilen = (
        OParlAgendaItem.objects.filter(meeting_id__in=ids)
        .annotate(hat_vorlage=Exists(mit_vorlage))
        .values_list("meeting_id", "id", "hat_vorlage")
    )
    anzahl: dict[uuid.UUID, int] = {}
    vorlagen: dict[uuid.UUID, set[uuid.UUID]] = {}
    for meeting_id, item_id, hat_vorlage in zeilen:
        anzahl[meeting_id] = anzahl.get(meeting_id, 0) + 1
        if hat_vorlage:
            vorlagen.setdefault(meeting_id, set()).add(item_id)
    return {mid: AgendaOverview(items=n, with_paper=frozenset(vorlagen.get(mid, ()))) for mid, n in anzahl.items()}


@dataclass(frozen=True)
class PaperOnAgenda:
    """Vorlage auf der Tagesordnung einer Sitzung: die Vorlage und die früheste Sitzung im gefragten Zeitraum."""

    paper: OParlPaper
    meeting: OParlMeeting


def papers_on_agendas(
    meetings_qs: QuerySet[OParlMeeting], *, limit: int = 5, exclude_papers: Iterable[uuid.UUID] = ()
) -> list[PaperOnAgenda]:
    """
    Vorlagen, die in den gegebenen Sitzungen beraten werden (ohne zurückgenommene Beratungen), neueste Vorlage
    zuerst, je Vorlage einmal mit der frühesten dieser Sitzungen. Zwei Abfragen.
    """
    sitzungen = {m.external_id: m for m in meetings_qs.order_by("start")}
    if not sitzungen:
        return []
    beratungen = (
        OParlConsultation.objects.filter(meeting_external_id__in=list(sitzungen), paper__isnull=False)
        .filter(consultation_meeting_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
        .exclude(paper_id__in=list(exclude_papers))
        .select_related("paper")
        .order_by("-paper__date", "-paper__oparl_created")
    )
    gefunden: dict[uuid.UUID, PaperOnAgenda] = {}
    for beratung in beratungen:
        sitzung = sitzungen.get(beratung.meeting_external_id or "")
        vorlage = beratung.paper
        if sitzung is None or vorlage is None:
            continue
        bisher = gefunden.get(vorlage.pk)
        if bisher is None:
            if len(gefunden) >= limit:
                continue
            gefunden[vorlage.pk] = PaperOnAgenda(paper=vorlage, meeting=sitzung)
        elif sitzung.start and bisher.meeting.start and sitzung.start < bisher.meeting.start:
            gefunden[vorlage.pk] = PaperOnAgenda(paper=vorlage, meeting=sitzung)
    return list(gefunden.values())


# =============================================================================
# Vorlagen
# =============================================================================


def papers(bodies: Bodies) -> QuerySet[OParlPaper]:
    """Vorlagen der Kommunen."""
    return OParlPaper.objects.filter(body__in=bodies)


def paper(bodies: Bodies, paper_id: object) -> OParlPaper | None:
    """Eine Vorlage der Kommunen; ``None``, wenn es sie dort nicht gibt."""
    pk = _uuid(paper_id)
    return papers(bodies).filter(pk=pk).first() if pk else None


def paper_exists(paper_id: object) -> bool:
    """Gibt es diese Vorlage im RIS-Bestand?"""
    pk = _uuid(paper_id)
    return pk is not None and OParlPaper.objects.filter(pk=pk).exists()


def search_papers(bodies: Bodies, text: str) -> QuerySet[OParlPaper]:
    """
    Vorlagen mit ``text`` im Namen oder in der Drucksachennummer, neueste zuerst.

    Sieht ``text`` wie eine Vorlagennummer aus, auch jede andere Schreibweise dieser Nummer; die passende Nummer steht
    dann vorn (``vorlagennummer.suchen``, Issue #1102).
    """
    return vorlagennummer.suchen(papers(bodies), text, reihenfolge=("-date",))


def search_papers_in(found: QuerySet[OParlPaper], text: str) -> QuerySet[OParlPaper]:
    """
    Vorgangslisten: ``found`` (Vorlagen der Kommunen samt Filtern des Aufrufers) eingeschränkt auf ``text`` in Name
    oder Nummer, bei einer Vorlagennummer in jeder Schreibweise und mit der passenden Nummer vorn, sonst neueste
    zuerst. Ohne ``text`` nur sortiert. Die eine Suche der Vorgangslisten in Insight und Work (Issue #1102).
    """
    if not text.strip():
        return found.order_by("-date", "-oparl_created")
    return vorlagennummer.suchen(found, text)


def public_papers_by_number(bodies: Bodies, text: str, *, limit: int = 10) -> list[OParlPaper]:
    """
    Nicht gelöschte, nicht zurückgenommene Vorlagen, deren Nummer zu ``text`` passt (jede Schreibweise, Issue #1102),
    genau passende zuerst, dann Folgevorlagen; leer, wenn ``text`` keine Vorlagennummer ist. Für Treffer, die die
    Suche oben anheftet (``nummer_rang`` je Vorlage).
    """
    if vorlagennummer.nummer(text) is None:
        return []
    return list(vorlagennummer.passend(public_papers(bodies).exclude(withdrawn_q()), text)[:limit])


def recent_papers(bodies: Bodies, *, limit: int = 5) -> QuerySet[OParlPaper]:
    """Neueste Vorlagen (nach Datum, dann Anlage im RIS)."""
    return papers(bodies).order_by("-date", "-oparl_created")[:limit]


def consultations_of_paper(paper: OParlPaper) -> QuerySet[OParlConsultation]:
    """Beratungsfolge einer Vorlage (ohne zurückgenommene Beratungen)."""
    return OParlConsultation.objects.filter(paper=paper).exclude(withdrawn_q())


def files_of_paper(paper: OParlPaper) -> QuerySet[OParlFile]:
    """Dateien (Anlagen) einer Vorlage."""
    return OParlFile.objects.filter(paper=paper)


def files_without_text() -> QuerySet[OParlFile]:
    """Dateien ohne Volltext und Rohdaten, etwa zum Vorladen für Seiten, die nur Name, Art und Größe zeigen."""
    return OParlFile.objects.defer("text_content", "raw_json")


#: Dokumentzeilen (Vorgang, Sitzung): Anfang des erkannten Texts, höchstens so viele Zeichen je Datei …
TEXTANFANG_ZEICHEN: Final = 100_000
#: … und alle Dateien einer Seite zusammen höchstens so viele (bei vielen Dateien bekommt jede denselben Anteil)
TEXTANFANG_JE_SEITE: Final = 1_000_000


def with_text_start(files: Iterable[OParlFile]) -> list[OParlFile]:
    """
    Anfang des erkannten Texts für Dokumentzeilen, ohne den Volltext zu laden: setzt an jeder Datei ``text_anfang``
    (höchstens ``TEXTANFANG_ZEICHEN`` Zeichen, alle zusammen höchstens ``TEXTANFANG_JE_SEITE``) und ``text_gekuerzt``
    (der Text geht weiter). Eine Abfrage für alle Dateien; gekürzt wird in der Datenbank (``Substr``).

    Die Dateien sollen ohne ``text_content`` geladen sein (``defer``): Der Volltext kann Millionen Zeichen lang sein
    und gehört nicht in den Speicher einer Seite.
    """
    liste = list(files)
    if not liste:
        return liste
    je_datei = max(1, min(TEXTANFANG_ZEICHEN, TEXTANFANG_JE_SEITE // len(liste)))
    # Ein Zeichen mehr: zeigt, ob der Text weitergeht
    anfaenge = dict(
        OParlFile.objects.filter(pk__in=[datei.pk for datei in liste])
        .annotate(anfang=Substr("text_content", 1, je_datei + 1))
        .values_list("pk", "anfang")
    )
    for datei in liste:
        anfang = anfaenge.get(datei.pk) or ""
        datei.text_anfang = anfang[:je_datei]  # type: ignore[attr-defined]
        datei.text_gekuerzt = len(anfang) > je_datei  # type: ignore[attr-defined]
    return liste


def files_of_papers(paper_ids: Iterable[object]) -> QuerySet[OParlFile]:
    """Dateien mehrerer Vorlagen in einer Abfrage (Seite einer Fraktionssitzung, Issue #1071)."""
    return OParlFile.objects.filter(paper_id__in=list(paper_ids))


def visible_files_of_papers(paper_ids: Iterable[object]) -> QuerySet[OParlFile]:
    """
    Abrufbare Dateien mehrerer Vorlagen in einer Abfrage, ohne Volltext und Rohdaten (Vorgangslisten, Issue #1103).

    Es gelten die Regeln der Vorgangsseiten: keine von Session zurückgenommenen Dateien und keine, die der Löschabgleich
    sperrt (in der Quelle gelöscht oder dort nicht mehr abrufbar, #787). Zurückgenommen setzt ``deleted`` voraus, die
    beiden Bedingungen decken also ``withdrawn_by_publisher`` und ``file_reconcile.is_blocked`` ab.
    """
    return OParlFile.objects.filter(
        paper_id__in=list(paper_ids), deleted=False, source_missing_since__isnull=True
    ).defer("text_content", "raw_json")


def protocol_file(meeting: OParlMeeting) -> OParlFile | None:
    """
    Öffentliche Niederschrift einer Sitzung (OParl ``resultsProtocol``, sonst ``verbatimProtocol``), Issue #318.

    Quelle ist das gespiegelte Meeting-Objekt; angezeigt wird nur eine nicht zurückgenommene Datei derselben Kommune
    (mandari Session veröffentlicht hier ausschließlich den öffentlichen Teil, eine Rücknahme markiert die Datei sofort
    als gelöscht). Sitzungsseiten von Insight und Work (Issue #853).
    """
    raw = meeting.raw_json if isinstance(meeting.raw_json, dict) else {}
    for key in ("resultsProtocol", "verbatimProtocol"):
        ref = raw.get(key)
        external_id = ref.get("id") if isinstance(ref, dict) else ref if isinstance(ref, str) else None
        if not external_id:
            continue
        found = (
            OParlFile.objects.filter(external_id=external_id, deleted=False).defer("text_content", "raw_json").first()
        )
        if found is not None and (found.body_id is None or found.body_id == meeting.body_id):
            return found
    return None


#: Sitzungsformate aus der OParl-Erweiterung von mandari Session (Issue #138)
BROADCAST_LABELS = {
    "hybrid": "Hybride Sitzung: Einzelne Mitglieder sind per Bild-Ton-Übertragung zugeschaltet.",
    "digital": "Digitale Sitzung: Die Mitglieder tagen per Videokonferenz.",
}


def broadcast_info(meeting: Any) -> dict[str, str] | None:
    """
    Sitzungsformat und Hinweis für die Öffentlichkeit (Übertragung, Anmeldung), Issue #138.

    Quelle ist die OParl-Erweiterung ``mandari:meetingFormat``/``mandari:publicAccess``. Die Daten stammen von einer
    externen Quelle: nur erwartete Typen, eigene Texte für das Format, Links nur mit http(s). Sitzungsseiten von
    Insight und Work (Issue #853).
    """
    raw = meeting.raw_json if isinstance(meeting.raw_json, dict) else {}
    access = raw.get("mandari:publicAccess")
    access = access if isinstance(access, dict) else {}
    url = access.get("url")
    hint = access.get("hint")
    format_ = raw.get("mandari:meetingFormat")
    info = {
        "label": BROADCAST_LABELS.get(format_, "") if isinstance(format_, str) else "",
        "url": url[:500] if isinstance(url, str) and url.startswith(("https://", "http://")) else "",
        "hint": hint[:1000] if isinstance(hint, str) else "",
    }
    return info if any(info.values()) else None


def files_of_meeting(meeting: OParlMeeting, *, ohne: Iterable[object] = ()) -> list[OParlFile]:
    """
    Dateien einer Sitzung (Einladung, Anlagen, Niederschrift) nach Name, ohne gelöschte und von mandari Session
    zurückgenommene; ``ohne`` lässt Dateien weg, die die Seite schon an anderer Stelle zeigt (etwa die Niederschrift).
    Ohne Volltext geladen; den Anfang für Dokumentzeilen liefert ``with_text_start``.
    """
    ausgelassen = [pk for pk in (_uuid(value) for value in ohne) if pk is not None]
    return list(
        OParlFile.objects.filter(meeting=meeting, deleted=False)
        .exclude(withdrawn_q())
        .exclude(pk__in=ausgelassen)
        .defer("text_content", "raw_json")
        .order_by("name", "file_name")
    )


def organization_count(meeting: OParlMeeting) -> int | None:
    """
    Anzahl der Gremien hinter ``OParlMeeting.get_display_name`` (Namen mit Komma verbunden) für den Stand-Satz.

    Aus den vorgeladenen Gremien und ohne weitere Abfrage; ``None``, wenn sie so nicht feststeht. Der Stand-Satz beugt
    danach auch Gremiennamen mit Komma („im Ausschuss für Planung, Bau und Umwelt“). Mehr als zwei zählt nicht: Für
    den Satz heißt es dann nur „mehrere“.
    """
    named = [org for org in list(meeting.organizations.all())[:2] if org.name]
    if named:
        return len(named)
    urls = meeting.raw_json.get("organization") if isinstance(meeting.raw_json, dict) else None
    return 1 if isinstance(urls, list) and len(urls) == 1 else None


def consultation_history(paper: OParlPaper) -> list[dict[str, Any]]:
    """
    Beratungsverlauf einer Vorlage, chronologisch, wie ihn der Stand-Satz und der Zeitstrahl
    (``insight_core.services.paper_status``) erwarten: je Beratung ``consultation``, ``meeting``, ``agenda_item``,
    ``date``, ``organization_name``, ``organization_count``, ``agenda_number``, ``result``, ``public``, ``role`` und
    ``authoritative``. Beratungen ohne bekannte Sitzung stehen an der Stelle von „jetzt“ (zwischen vergangenen und
    kommenden). Zurückgenommene Beratungen, Sitzungen und Tagesordnungspunkte fehlen – wie in der Session-OParl-API,
    die solche Verweise auslässt. Höchstens vier Abfragen.

    Grundlage der Vorgangsseiten von Insight und Work (Issue #853, vorher je eine eigene Abfrage).
    """
    consultations = list(with_reference_checks(OParlConsultation.objects.filter(paper=paper).exclude(withdrawn_q())))
    if not consultations:
        return []
    meeting_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]
    item_ids = [c.agenda_item_external_id for c in consultations if c.agenda_item_external_id]
    sitzungen: dict[str, OParlMeeting] = {}
    if meeting_ids:
        sitzungen = {
            m.external_id: m
            for m in OParlMeeting.objects.filter(external_id__in=meeting_ids)
            .exclude(withdrawn_q())
            .prefetch_related("organizations")
        }
    punkte: dict[str, OParlAgendaItem] = {}
    if item_ids:
        punkte = {
            a.external_id: a
            for a in OParlAgendaItem.objects.filter(external_id__in=item_ids)
            .exclude(withdrawn_q())
            .exclude(withdrawn_q("meeting"))
        }
    verlauf: list[dict[str, Any]] = []
    for consultation in consultations:
        # Sitzung bzw. Punkt einer anderen Quelle gilt als unbekannt
        sitzung = sitzungen.get(consultation.meeting_external_id or "") if consultation.meeting_in_source else None  # type: ignore[attr-defined]  # Annotation aus with_reference_checks
        punkt = punkte.get(consultation.agenda_item_external_id or "") if consultation.agenda_item_in_source else None  # type: ignore[attr-defined]  # Annotation aus with_reference_checks
        verlauf.append(
            {
                "consultation": consultation,
                "meeting": sitzung,
                "agenda_item": punkt,
                "date": sitzung.start if sitzung else None,
                "organization_name": sitzung.get_display_name() if sitzung else None,
                "organization_count": organization_count(sitzung) if sitzung else None,
                "agenda_number": punkt.number if punkt else None,
                "result": punkt.result if punkt else None,
                "public": punkt.public if punkt else True,
                "role": consultation.role,
                "authoritative": consultation.authoritative,
            }
        )
    jetzt = timezone.now()
    verlauf.sort(key=lambda eintrag: eintrag["date"] or jetzt)
    return verlauf


def agenda_items_of_papers(paper_ids: Iterable[object]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """
    Je Vorlage die Tagesordnungspunkte, unter denen sie beraten wird (ohne zurückgenommene Beratungen), etwa um
    Positionen einer Organisation den Vorlagen einer Liste zuzuordnen. Zwei Abfragen für alle Vorlagen.
    """
    ids = [pk for pk in (_uuid(value) for value in paper_ids) if pk is not None]
    if not ids:
        return {}
    beratungen = (
        OParlConsultation.objects.filter(paper_id__in=ids)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(agenda_item_external_id__isnull=True)
        .exclude(agenda_item_external_id="")
        .values_list("paper_id", "agenda_item_external_id")
    )
    vorlagen_je_punkt: dict[str, set[uuid.UUID]] = {}
    for paper_id, punkt in beratungen:
        if punkt:
            vorlagen_je_punkt.setdefault(punkt, set()).add(paper_id)
    ergebnis: dict[uuid.UUID, set[uuid.UUID]] = {}
    punkte = OParlAgendaItem.objects.filter(external_id__in=list(vorlagen_je_punkt)).values_list("id", "external_id")
    for item_id, external_id in punkte:
        for paper_id in vorlagen_je_punkt.get(external_id, ()):
            ergebnis.setdefault(paper_id, set()).add(item_id)
    return ergebnis


def papers_of_agenda_items(agenda_items: Iterable[OParlAgendaItem]) -> dict[uuid.UUID, list[OParlPaper]]:
    """
    Vorlagen je Tagesordnungspunkt (ohne zurückgenommene Beratungen und Vorlagen), in einer Abfrage für alle Punkte
    einer Sitzung statt einer je Punkt, je Punkt nach Vorlagen-Nr. und jede Vorlage einmal. Punkte ohne Vorlage fehlen.
    Sitzungsseiten von Insight und Work (Issue #853).
    """
    punkte = {item.external_id: item.pk for item in agenda_items if item.external_id}
    if not punkte:
        return {}
    beratungen = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=list(punkte), paper__isnull=False)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
        .select_related("paper")
        .order_by("paper__reference", "paper__name", "paper_id")
    )
    ergebnis: dict[uuid.UUID, list[OParlPaper]] = {}
    for beratung in beratungen:
        liste = ergebnis.setdefault(punkte[beratung.agenda_item_external_id or ""], [])
        if beratung.paper is not None and all(p.pk != beratung.paper_id for p in liste):
            liste.append(beratung.paper)
    return ergebnis


# =============================================================================
# Gremien und Personen
# =============================================================================


def organizations(bodies: Bodies) -> QuerySet[OParlOrganization]:
    """Gremien der Kommunen."""
    return OParlOrganization.objects.filter(body__in=bodies)


def organization(bodies: Bodies, organization_id: object) -> OParlOrganization | None:
    """Ein Gremium der Kommunen; ``None``, wenn es das dort nicht gibt."""
    pk = _uuid(organization_id)
    return organizations(bodies).filter(pk=pk).first() if pk else None


def active_organizations(bodies: Bodies, *, on: date | None = None) -> QuerySet[OParlOrganization]:
    """Gremien ohne Enddatum oder mit Enddatum ab ``on`` (Standard: heute)."""
    on = on or timezone.localdate()
    return organizations(bodies).filter(Q(end_date__isnull=True) | Q(end_date__gte=on))


def organizations_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlOrganization]:
    """Gremien zu OParl-Kennungen (URLs), etwa aus den Rohdaten einer Sitzung."""
    return OParlOrganization.objects.filter(external_id__in=list(external_ids))


def persons(bodies: Bodies) -> QuerySet[OParlPerson]:
    """Personen der Kommunen."""
    return OParlPerson.objects.filter(body__in=bodies)


def memberships_of_person(person: OParlPerson, *, bodies: Bodies | None = None) -> QuerySet[OParlMembership]:
    """Mitgliedschaften einer Person in Gremien, wahlweise nur in Gremien der Kommunen."""
    found = OParlMembership.objects.filter(person=person)
    if bodies is not None:
        found = found.filter(organization__body__in=bodies)
    return found


# =============================================================================
# Orte
# =============================================================================


def locations(bodies: Bodies) -> QuerySet[OParlLocation]:
    """Orte der Kommunen (eigene Location-Objekte der Quellen, nicht die Ortsangaben an Sitzungen)."""
    return OParlLocation.objects.filter(body__in=bodies)


@dataclass(frozen=True)
class PaperPlace:
    """Ein verorteter Vorgang auf einer Karte: Punkt, Ortsbezeichnung und Angaben der Vorlage."""

    paper_id: uuid.UUID
    latitude: float
    longitude: float
    place: str
    title: str
    reference: str
    paper_date: date | None
    paper_type: str = ""


#: Kartenausschnitt als (West, Süd, Ost, Nord) in Grad
Area = tuple[float, float, float, float]


def paper_places(
    bodies: Bodies, *, area: Area | None = None, since: date | None = None, limit: int = 2000
) -> list[PaperPlace]:
    """
    Verortungen von Vorgängen der Kommunen für Karten, neueste Vorgänge zuerst, höchstens ``limit`` Punkte.

    Liest die Tabelle der Verortungen (Index auf Kommune, Breite, Länge) statt des JSON am Vorgang, ohne entfernte
    und aus Datenschutzgründen ausgeblendete Verortungen (#1269) und ohne Vorgänge, die in der Quelle gelöscht oder zurückgenommen sind. ``area`` begrenzt auf einen
    Kartenausschnitt, ``since`` auf Vorgänge ab diesem Datum (Vorgänge ohne Datum fallen dann heraus). Eine Abfrage.
    """
    from insight_core.models import PaperLocation

    rows = PaperLocation.objects.filter(body__in=bodies, paper__deleted=False).exclude(
        status__in=PaperLocation.STATUS_UNSICHTBAR
    )
    if area is not None:
        west, south, east, north = area
        rows = rows.filter(latitude__gte=south, latitude__lte=north, longitude__gte=west, longitude__lte=east)
    if since is not None:
        rows = rows.filter(paper__date__gte=since)
    rows = rows.order_by(F("paper__date").desc(nulls_last=True), "paper_id", "pk")
    return [
        PaperPlace(
            paper_id=paper_id,
            latitude=float(lat),
            longitude=float(lon),
            place=str(place or ""),
            title=str(title or reference or "Vorgang"),
            reference=str(reference or ""),
            paper_date=paper_date,
            paper_type=str(paper_type or ""),
        )
        for paper_id, lat, lon, place, title, reference, paper_date, paper_type in rows.values_list(
            "paper_id",
            "latitude",
            "longitude",
            "name",
            "paper__name",
            "paper__reference",
            "paper__date",
            "paper__paper_type",
        )[: max(limit, 0)]
    ]


# =============================================================================
# Dateien
# =============================================================================


def files(bodies: Bodies) -> QuerySet[OParlFile]:
    """Dateien der Kommunen."""
    return OParlFile.objects.filter(body__in=bodies)


# =============================================================================
# Öffentlicher Bestand für den KI-Assistenten und offene Schnittstellen (Issue #899)
# =============================================================================
#
# Diese Abfragen liefern nur, was das Bürgerportal öffentlich zeigt, und eher weniger: keine gelöschten oder
# zurückgenommenen Einträge, keine Dateien, die die Kommune entfernt hat. Nichtöffentliche Tagesordnungspunkte
# erscheinen nur als Nummer mit Kennzeichen; Name, Ergebnis und Beschlusstext bleiben leer.


def public_body(body_id: object) -> OParlBody | None:
    """Eine nicht gelöschte Kommune; ``None`` bei ungültiger oder unbekannter Kennung."""
    pk = _uuid(body_id)
    return OParlBody.objects.filter(pk=pk, deleted=False).first() if pk else None


def listed_bodies(text: str = "") -> QuerySet[OParlBody]:
    """Gelistete Kommunen (Kommunenauswahl), wahlweise mit ``text`` im Namen oder Kurznamen, nach Namen sortiert."""
    found = OParlBody.objects.listed()
    if text:
        found = found.filter(
            Q(name__icontains=text) | Q(display_name__icontains=text) | Q(short_name__icontains=text) | Q(slug=text)
        )
    return found.order_by("name")


def listed_body(reference: str) -> OParlBody | None:
    """Eine gelistete Kommune zu Kurzname (``slug``) oder Kennung; ``None``, wenn es sie so nicht gibt."""
    reference = reference.strip()
    if not reference:
        return None
    pk = _uuid(reference)
    found = OParlBody.objects.listed()
    return (found.filter(pk=pk) if pk else found.filter(slug=reference.lower())).first()


def public_meetings_between(
    bodies: Bodies, start: datetime, end: datetime, *, organizations: Organizations | None = None
) -> QuerySet[OParlMeeting]:
    """
    Nicht gelöschte Sitzungen der Kommunen mit Beginn zwischen ``start`` und ``end`` (beide einschließlich), nach
    Beginn sortiert, mit vorgeladenen Gremien. Abgesagte Sitzungen bleiben drin (der Aufrufer kennzeichnet sie);
    mit ``organizations`` nur Sitzungen, an denen eines der Gremien beteiligt ist.
    """
    found = meetings(bodies).filter(deleted=False, start__gte=start, start__lte=end)
    if organizations is not None:
        found = found.filter(organizations__in=list(organizations)).distinct()
    return found.prefetch_related("organizations").order_by("start")


def public_meeting(bodies: Bodies, meeting_id: object) -> OParlMeeting | None:
    """Eine nicht gelöschte Sitzung der Kommunen mit vorgeladenen Gremien; sonst ``None``."""
    pk = _uuid(meeting_id)
    if pk is None:
        return None
    return meetings(bodies).filter(pk=pk, deleted=False).prefetch_related("organizations").first()


def _natural_key(number: str | None) -> list[tuple[int, int | str]]:
    """Sortierschlüssel für TOP-Nummern: 1, 2, 10 statt 1, 10, 2."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"(\d+)", number or "999") if part
    ]


@dataclass(frozen=True)
class AgendaEntry:
    """Tagesordnungspunkt mit den darunter beratenen öffentlichen Vorlagen."""

    item: OParlAgendaItem
    papers: list[OParlPaper]


def public_agenda(meeting: OParlMeeting) -> list[AgendaEntry]:
    """
    Tagesordnung einer Sitzung ohne gelöschte Punkte, in natürlicher Reihenfolge der Nummern; je Punkt die
    Vorlagen ohne zurückgenommene Beratungen und ohne gelöschte Vorlagen. Zwei Abfragen.
    """
    items = list(OParlAgendaItem.objects.filter(meeting=meeting, deleted=False))
    items.sort(key=lambda item: (item.order is None, item.order or 0, _natural_key(item.number)))
    by_item: dict[str, list[OParlPaper]] = {}
    if items:
        consultations = (
            OParlConsultation.objects.filter(
                agenda_item_external_id__in=[item.external_id for item in items],
                paper__isnull=False,
                paper__deleted=False,
            )
            .filter(consultation_agenda_item_in_source())
            .exclude(withdrawn_q())
            .select_related("paper")
        )
        for consultation in consultations:
            if consultation.paper is not None and consultation.agenda_item_external_id:
                found = by_item.setdefault(consultation.agenda_item_external_id, [])
                if consultation.paper not in found:
                    found.append(consultation.paper)
    return [AgendaEntry(item=item, papers=by_item.get(item.external_id, [])) for item in items]


def public_papers(bodies: Bodies) -> QuerySet[OParlPaper]:
    """Nicht gelöschte Vorlagen der Kommunen."""
    return papers(bodies).filter(deleted=False)


def public_paper(bodies: Bodies, paper_id: object) -> OParlPaper | None:
    """Eine nicht gelöschte Vorlage der Kommunen; sonst ``None``."""
    pk = _uuid(paper_id)
    return public_papers(bodies).filter(pk=pk).first() if pk else None


def public_papers_by_ids(bodies: Bodies, paper_ids: Iterable[object]) -> dict[uuid.UUID, OParlPaper]:
    """Nicht gelöschte Vorlagen der Kommunen zu Kennungen (etwa Treffer der Suche); unbekannte fehlen."""
    ids = [pk for pk in (_uuid(value) for value in paper_ids) if pk is not None]
    if not ids:
        return {}
    return {paper.pk: paper for paper in public_papers(bodies).filter(pk__in=ids)}


def public_papers_by_reference(bodies: Bodies, reference: str) -> QuerySet[OParlPaper]:
    """Nicht gelöschte Vorlagen mit genau dieser Drucksachennummer (ohne Groß-/Kleinschreibung), neueste zuerst."""
    reference = reference.strip()
    if not reference:
        return public_papers(bodies).none()
    return public_papers(bodies).filter(reference__iexact=reference).order_by("-date", "-oparl_created")


def search_public_papers(
    bodies: Bodies,
    text: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    organizations: Organizations | None = None,
) -> QuerySet[OParlPaper]:
    """
    Nicht gelöschte Vorlagen, deren Name oder Drucksachennummer jedes Wort aus ``text`` enthält, neueste zuerst.

    Datenbank-Rückfall, wenn die Volltextsuche nicht antwortet. Wahlweise mit Datum der Vorlage im Zeitraum und
    beraten in einer Sitzung eines der Gremien (ohne zurückgenommene Beratungen). Ist ``text`` eine Vorlagennummer,
    passt auch jede andere Schreibweise; die passende Nummer steht dann vorn (Issue #1102).
    """
    found = public_papers(bodies)
    woerter = Q()
    for word in text.split()[:8]:
        woerter &= Q(name__icontains=word) | Q(reference__icontains=word)
    if date_from is not None:
        found = found.filter(date__gte=date_from)
    if date_to is not None:
        found = found.filter(date__lte=date_to)
    if organizations is not None:
        sitzungen = OParlMeeting.objects.filter(organizations__in=list(organizations)).values("external_id")
        beraten = (
            OParlConsultation.objects.filter(paper=OuterRef("pk"), meeting_external_id__in=sitzungen)
            .filter(consultation_meeting_in_source())
            .exclude(withdrawn_q())
        )
        found = found.filter(Exists(beraten))
    return vorlagennummer.suchen(found, text, bedingung=woerter)


@dataclass(frozen=True)
class ConsultationStep:
    """
    Eine Beratung im Verlauf einer Vorlage, nur mit öffentlichen Angaben: Bei einem nichtöffentlichen
    Tagesordnungspunkt bleiben Ergebnis und Beschlusstext leer.
    """

    meeting_id: uuid.UUID | None
    date: datetime | None
    cancelled: bool
    organization_name: str | None
    agenda_number: str | None
    public: bool
    result: str | None
    resolution_text: str | None
    role: str | None
    authoritative: bool


def public_consultation_history(paper_ids: Iterable[object]) -> dict[uuid.UUID, list[ConsultationStep]]:
    """
    Beratungsverlauf je Vorlage (ohne zurückgenommene Beratungen, ohne gelöschte Sitzungen und Punkte),
    chronologisch, Beratungen ohne Termin zuletzt. Eine Abfrage für alle Vorlagen.
    """
    ids = [pk for pk in (_uuid(value) for value in paper_ids) if pk is not None]
    if not ids:
        return {}
    # Sitzung und Punkt nur aus der Quelle der Beratung
    sitzungen = OParlMeeting.objects.filter(
        meeting_in_consultation_source(), external_id=OuterRef("meeting_external_id"), deleted=False
    )
    punkte = OParlAgendaItem.objects.filter(
        agenda_item_in_consultation_source(),
        external_id=OuterRef("agenda_item_external_id"),
        deleted=False,
        meeting__deleted=False,
    )
    rows = (
        OParlConsultation.objects.filter(paper_id__in=ids)
        .exclude(withdrawn_q())
        .annotate(
            sitzung_id=Subquery(sitzungen.values("id")[:1]),
            sitzung_beginn=Subquery(sitzungen.values("start")[:1]),
            sitzung_abgesagt=Subquery(sitzungen.values("cancelled")[:1]),
            sitzung_name=Subquery(sitzungen.values("name")[:1]),
            gremium=Subquery(sitzungen.filter(organizations__name__gt="").values("organizations__name")[:1]),
            top_nummer=Subquery(punkte.values("number")[:1]),
            top_oeffentlich=Subquery(punkte.values("public")[:1]),
            top_ergebnis=Subquery(punkte.values("result")[:1]),
            top_beschluss=Subquery(punkte.values("resolution_text")[:1]),
        )
        .values(
            "paper_id",
            "role",
            "authoritative",
            "sitzung_id",
            "sitzung_beginn",
            "sitzung_abgesagt",
            "sitzung_name",
            "gremium",
            "top_nummer",
            "top_oeffentlich",
            "top_ergebnis",
            "top_beschluss",
        )
    )
    history: dict[uuid.UUID, list[ConsultationStep]] = {}
    for row in rows:
        public = row["top_oeffentlich"] is not False
        sitzung_name = row["sitzung_name"] or ""
        history.setdefault(row["paper_id"], []).append(
            ConsultationStep(
                meeting_id=row["sitzung_id"],
                date=row["sitzung_beginn"],
                cancelled=bool(row["sitzung_abgesagt"]),
                organization_name=row["gremium"] or (sitzung_name if sitzung_name.lower() != "sitzung" else None),
                agenda_number=row["top_nummer"],
                public=public,
                result=row["top_ergebnis"] if public else None,
                resolution_text=row["top_beschluss"] if public else None,
                role=row["role"],
                authoritative=bool(row["authoritative"]),
            )
        )
    for steps in history.values():
        steps.sort(key=lambda step: (step.date is None, step.date.timestamp() if step.date else 0.0))
    return history


def _public_files(bodies: Bodies) -> QuerySet[OParlFile]:
    """Dateien der Kommunen, die das Bürgerportal zeigt: nicht gelöscht, in der Quelle abrufbar, Vorgang/Sitzung da."""
    return (
        files(bodies)
        .filter(deleted=False, source_missing_since__isnull=True)
        .filter(Q(paper__isnull=True) | Q(paper__deleted=False))
        .filter(Q(meeting__isnull=True) | Q(meeting__deleted=False))
    )


def public_files_of_paper(paper: OParlPaper) -> QuerySet[OParlFile]:
    """Öffentliche Dateien einer Vorlage, ohne Volltext geladen."""
    return _public_files([paper.body_id]).filter(paper=paper).defer("text_content", "raw_json").order_by("name")


def public_files_by_ids(bodies: Bodies, file_ids: Iterable[object]) -> dict[uuid.UUID, OParlFile]:
    """Öffentliche Dateien der Kommunen zu Kennungen (etwa Treffer der Suche), ohne Volltext; unbekannte fehlen."""
    ids = [pk for pk in (_uuid(value) for value in file_ids) if pk is not None]
    if not ids:
        return {}
    found = _public_files(bodies).filter(pk__in=ids).defer("text_content", "raw_json").select_related("paper")
    return {datei.pk: datei for datei in found}


def public_file_with_text(bodies: Bodies, file_id: object, *, max_chars: int = MAX_TEXT_CHARS) -> OParlFile | None:
    """
    Eine öffentliche Datei der Kommunen samt erkanntem Text; sonst ``None``. Den Text liest
    ``hub.ris.canonical.file_text``: höchstens ``max_chars`` (und nie mehr als ``MAX_TEXT_CHARS``) Zeichen vom Anfang,
    gekürzt in der Datenbank; ``text_content`` selbst ist nicht geladen. ``text_laenge`` nennt die Länge des
    gespeicherten Texts.
    """
    pk = _uuid(file_id)
    if not pk:
        return None
    dateien = (
        _public_files(bodies)
        .filter(pk=pk)
        .defer("text_content", "raw_json")
        .annotate(
            text_begrenzt=Substr("text_content", 1, max(1, min(max_chars, MAX_TEXT_CHARS))),
            text_laenge=Length("text_content"),
        )
    )
    return cast(OParlFile | None, dateien.select_related("paper").first())


def public_organizations(bodies: Bodies, text: str = "", *, on: date | None = None) -> QuerySet[OParlOrganization]:
    """Nicht gelöschte, am Stichtag (Standard heute) bestehende Gremien der Kommunen, wahlweise mit ``text`` im Namen."""
    found = active_organizations(bodies, on=on).filter(deleted=False)
    if text:
        found = found.filter(Q(name__icontains=text) | Q(short_name__icontains=text))
    return found.order_by("name")


def public_organizations_named(bodies: Bodies, name: str) -> list[OParlOrganization]:
    """
    Gremien zu einem Namen aus einer Frage: genau gleich (auch Kurzname, ohne Groß-/Kleinschreibung), sonst
    höchstens zehn, deren Name ihn enthält. Bestehende Gremien zuerst, aufgelöste nur ohne Treffer.
    """
    name = name.strip()
    if not name:
        return []
    alle = organizations(bodies).filter(deleted=False)
    for found in (active_organizations(bodies).filter(deleted=False), alle):
        exact = list(found.filter(Q(name__iexact=name) | Q(short_name__iexact=name))[:10])
        if exact:
            return exact
        partial = list(found.filter(Q(name__icontains=name) | Q(short_name__icontains=name)).order_by("name")[:10])
        if partial:
            return partial
    return []


def search_public_persons(bodies: Bodies, name: str) -> QuerySet[OParlPerson]:
    """Nicht gelöschte Personen der Kommunen, deren Name jedes Wort aus ``name`` enthält."""
    found = persons(bodies).filter(deleted=False)
    for word in name.split()[:4]:
        found = found.filter(Q(name__icontains=word) | Q(given_name__icontains=word) | Q(family_name__icontains=word))
    return found.order_by("family_name", "name")


def public_current_memberships(person: OParlPerson, *, on: date | None = None) -> QuerySet[OParlMembership]:
    """Laufende Mitgliedschaften einer Person in nicht gelöschten Gremien ihrer Kommune (Stichtag Standard heute)."""
    on = on or timezone.localdate()
    return (
        OParlMembership.objects.filter(person=person, deleted=False, organization__deleted=False)
        .filter(organization__body_id=person.body_id)
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=on))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=on))
        .select_related("organization")
        .order_by("organization__name")
    )


# =============================================================================
# Weg eines Vorgangs: verbundene Vorlagen, Federführung, Texte (Issue #1273)
# =============================================================================

#: OParl-Felder einer Vorlage mit Verweisen auf andere Vorlagen: übergeordnete Vorlage (etwa die Anfrage zu einer
#: Stellungnahme), Folgevorlagen (Stellungnahmen, Änderungsanträge, Ergänzungen) und sonst verwandte Vorlagen
VORLAGEN_VERWEISE: Final = ("superordinatedPaper", "subordinatedPaper", "relatedPaper")
#: Höchstens so viele verbundene Vorlagen je Feld (die Seite bleibt lesbar, die Abfrage klein)
MAX_VERBUNDENE_VORLAGEN: Final = 20


def oparl_references(raw: object, key: str) -> list[str]:
    """
    Kennungen (URLs) eines OParl-Verweisfelds aus den Rohdaten, in der Reihenfolge der Quelle und ohne Dubletten.

    Ein Feld ist eine Liste aus URLs oder eingebetteten Objekten mit ``id``; manche Systeme liefern einen einzelnen
    Wert statt einer Liste. Alles andere zählt nicht.
    """
    value = raw.get(key) if isinstance(raw, dict) else None
    values = value if isinstance(value, list) else [value]
    found: list[str] = []
    for item in values:
        reference = item.get("id") if isinstance(item, dict) else item
        if isinstance(reference, str) and reference.strip() and reference.strip() not in found:
            found.append(reference.strip())
    return found


def related_public_papers(paper: OParlPaper) -> dict[str, list[OParlPaper]]:
    """
    Verbundene Vorlagen laut OParl je Feld aus ``VORLAGEN_VERWEISE``, in einer Abfrage für alle Felder.

    Nur nicht gelöschte und nicht zurückgenommene Vorlagen derselben Kommune, ohne die Vorlage selbst, je Feld
    höchstens ``MAX_VERBUNDENE_VORLAGEN`` und nach Datum (ohne Datum zuletzt). Verweise auf Vorlagen, die nicht im
    Bestand liegen, fehlen. Felder ohne Treffer stehen mit leerer Liste da.
    """
    raw = paper.raw_json if isinstance(paper.raw_json, dict) else {}
    per_key = {key: oparl_references(raw, key)[:MAX_VERBUNDENE_VORLAGEN] for key in VORLAGEN_VERWEISE}
    wanted = {reference for references in per_key.values() for reference in references}
    wanted.discard(paper.external_id)
    if not wanted:
        return {key: [] for key in VORLAGEN_VERWEISE}
    found = {
        other.external_id: other
        for other in public_papers([paper.body_id])
        .filter(external_id__in=list(wanted))
        .exclude(withdrawn_q())
        .exclude(pk=paper.pk)
        .defer("summary", "summary_einfach", "locations")
    }

    def _order(other: OParlPaper) -> tuple[bool, date]:
        return (other.date is None, other.date or date.min)

    return {
        key: sorted((found[reference] for reference in references if reference in found), key=_order)
        for key, references in per_key.items()
    }


def organization_names_by_external_id(body_id: object, external_ids: Iterable[str]) -> dict[str, str]:
    """
    Namen nicht gelöschter Gremien und Organisationseinheiten einer Kommune zu OParl-Kennungen, etwa aus
    ``underDirectionOf``. Kennungen, die auf etwas anderes zeigen (manche Systeme nennen dort eine Person), und
    unbekannte fehlen; Personen geben wir so nie aus.
    """
    ids = [external_id for external_id in dict.fromkeys(external_ids) if external_id]
    pk = _uuid(body_id)
    if not ids or pk is None:
        return {}
    rows = (
        OParlOrganization.objects.filter(body_id=pk, external_id__in=ids, deleted=False)
        .exclude(name__isnull=True)
        .exclude(name="")
        .values_list("external_id", "name")
    )
    return {external_id: " ".join(name.split()) for external_id, name in rows if name}


def text_starts_of_files(file_ids: Iterable[object], *, max_chars: int) -> dict[uuid.UUID, str]:
    """
    Anfang des erkannten Texts ausgewählter Dateien je Primärschlüssel (höchstens ``max_chars`` Zeichen und nie mehr
    als ``MAX_TEXT_CHARS``, gekürzt in der Datenbank). Es gelten die Regeln der Vorgangsseiten
    (``visible_files_of_papers``); Dateien ohne Text stehen mit leerem Text da. Eine Abfrage nur für die genannten
    Dateien, ``text_content`` selbst ist nicht geladen.
    """
    ids = [pk for pk in (_uuid(value) for value in file_ids) if pk is not None]
    if not ids:
        return {}
    rows = (
        OParlFile.objects.filter(pk__in=ids, deleted=False, source_missing_since__isnull=True)
        .annotate(text_begrenzt=Substr("text_content", 1, max(1, min(max_chars, MAX_TEXT_CHARS))))
        .values_list("pk", "text_begrenzt")
    )
    return {pk: text or "" for pk, text in rows}
