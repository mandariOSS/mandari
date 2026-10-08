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

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

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
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
    )
    return OParlPaper.objects.filter(id__in=consultations.values("paper_id")).distinct()


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
    """Vorlagen mit ``text`` im Namen oder in der Drucksachennummer, neueste zuerst."""
    return papers(bodies).filter(Q(name__icontains=text) | Q(reference__icontains=text)).order_by("-date")


def recent_papers(bodies: Bodies, *, limit: int = 5) -> QuerySet[OParlPaper]:
    """Neueste Vorlagen (nach Datum, dann Anlage im RIS)."""
    return papers(bodies).order_by("-date", "-oparl_created")[:limit]


def consultations_of_paper(paper: OParlPaper) -> QuerySet[OParlConsultation]:
    """Beratungsfolge einer Vorlage (ohne zurückgenommene Beratungen)."""
    return OParlConsultation.objects.filter(paper=paper).exclude(withdrawn_q())


def files_of_paper(paper: OParlPaper) -> QuerySet[OParlFile]:
    """Dateien (Anlagen) einer Vorlage."""
    return OParlFile.objects.filter(paper=paper)


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
    """
    ausgelassen = [pk for pk in (_uuid(value) for value in ohne) if pk is not None]
    return list(
        OParlFile.objects.filter(meeting=meeting, deleted=False)
        .exclude(withdrawn_q())
        .exclude(pk__in=ausgelassen)
        .defer("raw_json")
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
    consultations = list(OParlConsultation.objects.filter(paper=paper).exclude(withdrawn_q()))
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
        sitzung = sitzungen.get(consultation.meeting_external_id or "")
        punkt = punkte.get(consultation.agenda_item_external_id or "")
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
    einer Sitzung statt einer je Punkt. Punkte ohne Vorlage fehlen.
    """
    punkte = {item.external_id: item.pk for item in agenda_items if item.external_id}
    if not punkte:
        return {}
    beratungen = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=list(punkte), paper__isnull=False)
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


# =============================================================================
# Dateien
# =============================================================================


def files(bodies: Bodies) -> QuerySet[OParlFile]:
    """Dateien der Kommunen."""
    return OParlFile.objects.filter(body__in=bodies)
