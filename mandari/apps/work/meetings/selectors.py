# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesende Zugriffe für die Sitzungsvorbereitung (Issue #160, Service-Layer).

Alle Querysets sind an eine Organisation (bzw. deren Körperschaften oder eine
Mitgliedschaft) gebunden. Die Views reichen nur noch IDs und Filter durch; die
Vorbereitungsseite bekommt ihre Daten gebündelt als ``PreparationData``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Count, Q, QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
)

from .models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
    PaperComment,
)
from .serializers import decrypted

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization
    from apps.work.motions.models import Motion

MEETING_LIST_LIMIT = 100
MEETING_LIST_HORIZON_DAYS = 180


# ---------------------------------------------------------------------------
# Typisierte Hüllen um untypisierte Modell-Helfer
# ---------------------------------------------------------------------------


def organization_bodies(organization: Organization | None) -> QuerySet[OParlBody] | None:
    """Verknüpfte Körperschaften der Organisation; ``None``, wenn keine vorhanden ist."""
    if organization is None:
        return None
    bodies = cast("QuerySet[OParlBody]", cast(Any, organization).get_all_bodies())
    if not bodies.exists():
        return None
    return bodies


def papers_of_item(agenda_item: OParlAgendaItem) -> list[OParlPaper]:
    """Alle von einem TOP beratenen Vorlagen (nutzt vorgeladene Daten, wenn vorhanden)."""
    return list(cast(Any, agenda_item).get_papers())


def attach(obj: object, **attrs: Any) -> None:
    """Berechnete Attribute für Templates an ein Modellobjekt hängen (z. B. ``committee_name``)."""
    for name, value in attrs.items():
        setattr(obj, name, value)


# ---------------------------------------------------------------------------
# Sitzungen und Tagesordnungspunkte
# ---------------------------------------------------------------------------


def get_meeting_or_404(bodies: QuerySet[OParlBody], meeting_id: Any, *, with_agenda: bool = False) -> OParlMeeting:
    """Sitzung innerhalb der Körperschaften der Organisation, sonst 404."""
    queryset = OParlMeeting.objects.all()
    if with_agenda:
        queryset = queryset.prefetch_related("organizations", "agenda_items")
    return get_object_or_404(queryset, id=meeting_id, body__in=bodies)


def get_agenda_item_or_404(item_id: Any, meeting: OParlMeeting | None = None) -> OParlAgendaItem:
    """Tagesordnungspunkt per ID (optional an eine Sitzung gebunden), sonst 404."""
    if meeting is not None:
        return get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)
    return get_object_or_404(OParlAgendaItem, id=item_id)


def find_agenda_item(item_id: Any) -> OParlAgendaItem | None:
    """Tagesordnungspunkt per ID oder ``None``."""
    return OParlAgendaItem.objects.filter(id=item_id).first()


def natural_sort_key(item: OParlAgendaItem) -> list[tuple[int, int | str]]:
    """TOPs natürlich sortieren: 1, 2, 10, 11 statt 1, 10, 11, 2."""
    number = item.number or "999"
    parts = re.split(r"(\d+)", str(number))
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p]


def sorted_agenda_items(meeting: OParlMeeting) -> list[OParlAgendaItem]:
    """Tagesordnung einer Sitzung in natürlicher Reihenfolge."""
    return sorted(meeting.agenda_items.all(), key=natural_sort_key)


def committee_name_for_meeting(meeting: OParlMeeting, org_cache: dict[str, str | None]) -> str | None:
    """Gremienname einer Sitzung (Relation, ersatzweise Rohdaten mit Nachschlage-Cache)."""
    try:
        orgs = meeting.organizations.all()
        if orgs:
            return orgs[0].name
    except Exception:  # noqa: BLE001 — defekte Relationen dürfen die Liste nicht brechen
        pass

    try:
        raw = meeting.raw_json or {}
        orgs_raw = raw.get("organization", [])
        if isinstance(orgs_raw, list) and orgs_raw:
            org_url = str(orgs_raw[0])
            if org_url in org_cache:
                return org_cache[org_url]
            org_obj = OParlOrganization.objects.filter(external_id=org_url).first()
            if org_obj:
                org_cache[org_url] = org_obj.name
                return org_obj.name
    except Exception:  # noqa: BLE001 — Rohdaten sind nicht schemasicher
        pass

    return None


def get_primary_paper_for_item(agenda_item: OParlAgendaItem | None) -> OParlPaper | None:
    """Erste Vorlage eines TOPs (über OParlConsultation) oder None."""
    if agenda_item is None or not agenda_item.external_id:
        return None
    consultation = (
        OParlConsultation.objects.filter(agenda_item_external_id=agenda_item.external_id, paper__isnull=False)
        .select_related("paper")
        .first()
    )
    return consultation.paper if consultation else None


def prefetch_papers_for_agenda_items(agenda_items: list[OParlAgendaItem]) -> dict[Any, list[OParlPaper]]:
    """
    Vorlagen für eine Liste von TOPs über Consultations vorladen.

    Liefert ``{agenda_item.id: [OParlPaper, ...]}``; jede Vorlage trägt
    ``_prefetched_consultation_count`` (Anzahl Beratungen) für die Anzeige.
    """
    if not agenda_items:
        return {}

    external_ids = [item.external_id for item in agenda_items if item.external_id]
    if not external_ids:
        return {}

    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=external_ids)
        .select_related("paper")
        .prefetch_related("paper__files", "paper__consultations")
    )

    paper_ids = {consultation.paper.id for consultation in consultations if consultation.paper}

    paper_consultation_counts: dict[Any, int] = {}
    if paper_ids:
        papers_with_counts = OParlPaper.objects.filter(id__in=paper_ids).annotate(
            consultation_count=Count("consultations")
        )
        paper_consultation_counts = {p.id: int(getattr(p, "consultation_count", 0)) for p in papers_with_counts}

    papers_by_ext_id: dict[str, list[OParlPaper]] = {}
    for consultation in consultations:
        if consultation.paper and consultation.agenda_item_external_id:
            ext_id = consultation.agenda_item_external_id
            bucket = papers_by_ext_id.setdefault(ext_id, [])
            if consultation.paper not in bucket:
                attach(
                    consultation.paper,
                    _prefetched_consultation_count=paper_consultation_counts.get(consultation.paper.id, 0),
                )
                bucket.append(consultation.paper)

    return {item.id: papers_by_ext_id.get(item.external_id, []) if item.external_id else [] for item in agenda_items}


# ---------------------------------------------------------------------------
# Sitzungsliste, Kalender, Detail
# ---------------------------------------------------------------------------


def assigned_committees(membership: Membership | None, bodies: QuerySet[OParlBody]) -> list[OParlOrganization]:
    """Dem Mitglied zugewiesene Gremien innerhalb der Körperschaften."""
    if membership is None:
        return []
    return list(membership.oparl_committees.filter(body__in=bodies))


def meetings_for_list(bodies: QuerySet[OParlBody], time_filter: str, now: datetime) -> list[OParlMeeting]:
    """Sitzungen der Körperschaften nach Zeitfilter (``upcoming``/``past``/alle), max. 100, 180-Tage-Fenster."""
    meetings_qs = OParlMeeting.objects.filter(body__in=bodies).prefetch_related("organizations")

    if time_filter == "upcoming":
        meetings_qs = meetings_qs.filter(start__gte=now - timedelta(hours=2)).order_by("start")
    elif time_filter == "past":
        meetings_qs = meetings_qs.filter(start__lt=now).order_by("-start")
    else:
        meetings_qs = meetings_qs.order_by("-start")

    if time_filter == "upcoming":
        meetings_qs = meetings_qs.filter(start__lte=now + timedelta(days=MEETING_LIST_HORIZON_DAYS))
    elif time_filter == "past":
        meetings_qs = meetings_qs.filter(start__gte=now - timedelta(days=MEETING_LIST_HORIZON_DAYS))

    return list(meetings_qs[:MEETING_LIST_LIMIT])


def filter_meetings(
    meetings: list[OParlMeeting],
    *,
    committee_ids: list[Any],
    committee_filter: str,
    search_query: str,
) -> list[OParlMeeting]:
    """Sitzungen im Speicher nach eigenen Gremien, Gremien-Filter und Suchbegriff eingrenzen."""
    if committee_ids:
        meetings = [m for m in meetings if any(org.id in committee_ids for org in m.organizations.all())]
    if committee_filter:
        meetings = [m for m in meetings if any(str(org.id) == committee_filter for org in m.organizations.all())]
    if search_query:
        q = search_query.lower()
        meetings = [m for m in meetings if q in (m.name or "").lower()]
    return meetings


def prepared_meeting_ids(organization: Organization) -> set[Any]:
    """IDs der Sitzungen, die die Organisation bereits in Vorbereitung hat."""
    return set(
        MeetingPreparation.objects.filter(organization=organization, is_prepared=True).values_list(
            "meeting_id", flat=True
        )
    )


def annotate_meetings_for_list(meetings: list[OParlMeeting], organization: Organization) -> None:
    """``committee_name`` und ``is_user_prepared`` für die Listenansicht anhängen."""
    org_cache: dict[str, str | None] = {}
    prepared_ids = prepared_meeting_ids(organization)
    for meeting in meetings:
        attach(
            meeting,
            committee_name=committee_name_for_meeting(meeting, org_cache),
            is_user_prepared=meeting.id in prepared_ids,
        )


def committee_choices(bodies: QuerySet[OParlBody]) -> list[dict[str, Any]]:
    """Alle Ausschüsse der Körperschaften für das Filter-Dropdown."""
    rows = (
        OParlOrganization.objects.filter(body__in=bodies, organization_type__icontains="committee")
        .order_by("name")
        .values("id", "name")
    )
    return cast("list[dict[str, Any]]", list(rows))


def meetings_between(bodies: QuerySet[OParlBody], start: datetime, end: datetime) -> QuerySet[OParlMeeting]:
    """Sitzungen der Körperschaften in einem Zeitfenster (Kalender)."""
    return OParlMeeting.objects.filter(body__in=bodies, start__gte=start, start__lte=end).prefetch_related(
        "organizations"
    )


def get_preparation(organization: Organization, meeting: OParlMeeting) -> MeetingPreparation | None:
    """Org-weite Vorbereitung einer Sitzung oder ``None``."""
    return MeetingPreparation.objects.filter(organization=organization, meeting=meeting).first()


# ---------------------------------------------------------------------------
# Sektionen pro TOP
# ---------------------------------------------------------------------------


def get_position(organization: Organization, agenda_item: OParlAgendaItem) -> AgendaItemPosition | None:
    """Org-weite Position zu einem TOP."""
    return AgendaItemPosition.objects.filter(organization=organization, agenda_item=agenda_item).first()


def cross_positions(organization: Organization, agenda_items: list[OParlAgendaItem]) -> dict[Any, list[dict[str, Any]]]:
    """Positionen derselben Organisation aus anderen Gremien zur selben Vorlage."""
    return cast(
        "dict[Any, list[dict[str, Any]]]",
        cast(Any, AgendaItemPosition).get_cross_positions_for_items(organization, agenda_items),
    )


def get_private_note(membership: Membership, item_id: Any) -> AgendaPrivateNote | None:
    """Private Notiz des Mitglieds zu einem TOP."""
    return AgendaPrivateNote.objects.filter(author=membership, agenda_item_id=item_id).first()


def get_own_speech(membership: Membership, agenda_item: OParlAgendaItem) -> AgendaSpeechNote | None:
    """Eigener Redebeitrag zu einem TOP (inkl. verknüpftem Dokument)."""
    return (
        AgendaSpeechNote.objects.filter(author=membership, agenda_item=agenda_item)
        .select_related("linked_document")
        .first()
    )


def shared_speeches(
    organization: Organization, agenda_item: OParlAgendaItem, exclude_author: Membership
) -> QuerySet[AgendaSpeechNote]:
    """Mit der Organisation geteilte Redebeiträge anderer Mitglieder zu einem TOP."""
    return (
        AgendaSpeechNote.objects.filter(organization=organization, agenda_item=agenda_item, is_shared=True)
        .exclude(author=exclude_author)
        .select_related("author__user")
    )


def speech_content_for(note: AgendaSpeechNote | None, membership: Membership) -> str:
    """Redetext für die Ausgabe: verknüpftes Dokument (mit can_access) oder eigener Inhalt."""
    if note is None:
        return ""
    if note.linked_document_id:
        doc = note.linked_document
        if doc is not None and doc.can_access(membership):
            return decrypted(doc, "content")
        return ""
    return decrypted(note, "content")


def linkable_documents(membership: Membership, query: str, *, limit: int = 50) -> list[Motion]:
    """Dokumente der Organisation, die als Redebeitrag verknüpfbar sind (Sichtbarkeit des Mitglieds)."""
    from apps.work.motions.models import Motion

    docs = cast("QuerySet[Motion]", cast(Any, Motion).visible_to(membership)).order_by("-updated_at")
    if query:
        docs = docs.filter(title__icontains=query)
    return list(docs[:limit])


def find_linkable_document(organization: Organization, membership: Membership, document_id: Any) -> Motion | None:
    """Dokument der Organisation, auf das das Mitglied Zugriff hat, sonst ``None``."""
    from apps.work.motions.models import Motion

    document = Motion.objects.filter(id=document_id, organization=organization).first()
    if document is None or not document.can_access(membership):
        return None
    return document


def thread_notes(organization: Organization, item_id: Any) -> list[AgendaItemNote]:
    """Org-lokale Diskussionsnotizen eines TOPs (migrierte ausgeschlossen)."""
    return list(
        AgendaItemNote.objects.filter(
            organization=organization,
            agenda_item_id=item_id,
            migrated_to_paper_comment__isnull=True,
        )
        .select_related("author", "author__user")
        .order_by("-is_pinned", "-is_decision", "-created_at")
    )


def consulting_notes(
    organization: Organization, agenda_items: list[OParlAgendaItem]
) -> dict[Any, list[AgendaItemNote]]:
    """Consulting-Notizen aus Vorbereitungen anderer Gremien zur selben Vorlage (mit ``origin_meeting``)."""
    return cast(
        "dict[Any, list[AgendaItemNote]]",
        cast(Any, AgendaItemNote).get_consulting_notes_for_items(organization, agenda_items),
    )


def visible_paper_comments(paper: OParlPaper, membership: Membership) -> list[PaperComment]:
    """Für das Mitglied sichtbare Kommentare einer Vorlage."""
    return cast("list[PaperComment]", cast(Any, PaperComment).get_visible_comments_for_paper(paper, membership))


def documents_with_annotation_counts(
    organization: Organization, agenda_item: OParlAgendaItem
) -> list[tuple[AgendaSupplementaryDocument, int]]:
    """Sichtbare Anlagen eines TOPs (inkl. geteilter Vorlagen-Anhänge) mit Anmerkungs-Zähler."""
    docs = list(
        cast(
            "QuerySet[AgendaSupplementaryDocument]",
            cast(Any, AgendaSupplementaryDocument).visible_for_item(organization, agenda_item),
        )
    )
    if not docs:
        return []

    doc_counts = {
        row["supplementary_document"]: row["c"]
        for row in FileAnnotation.objects.filter(
            organization=organization, supplementary_document_id__in=[d.id for d in docs]
        )
        .values("supplementary_document")
        .annotate(c=Count("id"))
    }
    oparl_ids = [d.oparl_file_id for d in docs if d.oparl_file_id]
    oparl_counts: dict[Any, int] = {}
    if oparl_ids:
        oparl_counts = {
            row["oparl_file"]: row["c"]
            for row in FileAnnotation.objects.filter(organization=organization, oparl_file_id__in=oparl_ids)
            .values("oparl_file")
            .annotate(c=Count("id"))
        }

    def annotation_count(d: AgendaSupplementaryDocument) -> int:
        if d.document_type == "oparl" and d.oparl_file_id:
            return int(oparl_counts.get(d.oparl_file_id, 0))
        return int(doc_counts.get(d.id, 0))

    return [(d, annotation_count(d)) for d in docs]


def resolve_file_anchor(organization: Organization | None, anchor_type: str | None, file_id: Any) -> dict[str, Any]:
    """
    Anker einer Datei-Anmerkung auflösen; außerhalb der Org-Grenze wird 404 geworfen.

    ``oparl``: RIS-Datei einer verknüpften Körperschaft, ``doc``: eigene Anlage.
    """
    if anchor_type == "oparl":
        bodies = organization_bodies(organization)
        if bodies is None:
            raise Http404
        file_obj = get_object_or_404(
            OParlFile.objects.filter(
                Q(body__in=bodies) | Q(paper__body__in=bodies) | Q(meeting__body__in=bodies)
            ).distinct(),
            id=file_id,
        )
        return {"oparl_file": file_obj}
    if anchor_type == "doc":
        doc = get_object_or_404(AgendaSupplementaryDocument, id=file_id, organization=organization)
        return {"supplementary_document": doc}
    raise Http404


def file_annotations(organization: Organization, anchor: dict[str, Any]) -> list[FileAnnotation]:
    """Alle Anmerkungen der Organisation an einem Anker."""
    return list(
        FileAnnotation.objects.filter(organization=organization, **anchor).select_related("author", "author__user")
    )


def count_file_annotations(organization: Organization, anchor: dict[str, Any]) -> int:
    """Anzahl der Anmerkungen der Organisation an einem Anker."""
    return int(FileAnnotation.objects.filter(organization=organization, **anchor).count())


# ---------------------------------------------------------------------------
# Zusammenfassung
# ---------------------------------------------------------------------------


def positions_for_meeting(organization: Organization, meeting: OParlMeeting) -> QuerySet[AgendaItemPosition]:
    """Alle Positionen der Organisation zu einer Sitzung."""
    return AgendaItemPosition.objects.filter(organization=organization, agenda_item__meeting=meeting).select_related(
        "agenda_item", "set_by", "set_by__user"
    )


def shared_speeches_for_meeting(organization: Organization, meeting: OParlMeeting) -> QuerySet[AgendaSpeechNote]:
    """Geteilte Redebeiträge der Organisation zu einer Sitzung, nach TOP-Nummer."""
    return (
        AgendaSpeechNote.objects.filter(organization=organization, agenda_item__meeting=meeting, is_shared=True)
        .select_related("agenda_item", "author__user")
        .order_by("agenda_item__number")
    )


def group_positions_by_type(
    positions: QuerySet[AgendaItemPosition],
) -> tuple[dict[str, list[AgendaItemPosition]], list[dict[str, Any]], bool]:
    """Positionen nach Positionsart gruppieren (alle Arten inkl. ``open``) und Abschnitte ohne ``open`` bilden."""
    positions_by_type: dict[str, list[AgendaItemPosition]] = {
        code: [] for code, _label in AgendaItemPosition.POSITION_CHOICES
    }
    for pos in positions:
        if pos.position in positions_by_type:
            positions_by_type[pos.position].append(pos)
    sections = [
        {"code": code, "label": label, "positions": positions_by_type[code]}
        for code, label in AgendaItemPosition.POSITION_CHOICES
        if code != "open"
    ]
    has_positions = any(section["positions"] for section in sections)
    return positions_by_type, sections, has_positions


# ---------------------------------------------------------------------------
# Vorbereitungsseite
# ---------------------------------------------------------------------------


@dataclass
class PreparedItem:
    """Alle Sektionen eines TOPs für die Vorbereitungsseite."""

    item: OParlAgendaItem
    position: AgendaItemPosition | None
    private_note: AgendaPrivateNote | None
    own_speech: AgendaSpeechNote | None
    shared_speeches: list[AgendaSpeechNote]
    notes: list[AgendaItemNote]
    documents: list[AgendaSupplementaryDocument]
    papers: list[OParlPaper]
    primary_paper: OParlPaper | None
    has_files: bool

    def as_template_dict(self) -> dict[str, Any]:
        """Darstellung für das Template (bisherige Dict-Struktur ``prepared_items``)."""
        return {
            "item": self.item,
            "position": self.position,
            "private_note": self.private_note,
            "own_speech": self.own_speech,
            "shared_speeches": self.shared_speeches,
            "notes": self.notes,
            "documents": self.documents,
            "papers": self.papers,
            "primary_paper": self.primary_paper,
            "has_files": self.has_files,
        }


@dataclass
class PreparationData:
    """Gebündelte Daten der Vorbereitungsseite einer Sitzung."""

    agenda_items: list[OParlAgendaItem]
    prepared_items: list[PreparedItem]
    cross_positions: dict[Any, list[dict[str, Any]]] = field(default_factory=dict)
    file_annotation_counts: dict[Any, int] = field(default_factory=dict)
    consultations_by_paper: dict[Any, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def stats(self) -> dict[str, int]:
        """Kennzahlen der Vorbereitung (Positionen, Redebeiträge, private Notizen)."""
        return {
            "total_items": len(self.agenda_items),
            "positioned": len([i for i in self.prepared_items if i.position and i.position.position != "open"]),
            "want_to_speak": len([i for i in self.prepared_items if i.own_speech]),
            "with_notes": len([i for i in self.prepared_items if i.private_note]),
        }


def _documents_by_item(
    organization: Organization, agenda_items: list[OParlAgendaItem], papers_by_item: dict[Any, list[OParlPaper]]
) -> dict[Any, list[AgendaSupplementaryDocument]]:
    """Direkte TOP-Anhänge plus über Gremien geteilte Vorlagen-Anhänge der eigenen Organisation."""
    all_paper_ids = {p.id for papers in papers_by_item.values() for p in papers}
    docs_qs = (
        AgendaSupplementaryDocument.objects.filter(organization=organization)
        .filter(Q(agenda_item__in=agenda_items) | Q(paper_id__in=all_paper_ids, share_across_committees=True))
        .select_related("added_by__user", "oparl_file")
    )
    item_ids = {item.id for item in agenda_items}
    items_by_paper: dict[Any, list[Any]] = {}
    for item in agenda_items:
        for p in papers_by_item.get(item.id, []):
            items_by_paper.setdefault(p.id, []).append(item.id)

    docs_by_item: dict[Any, list[AgendaSupplementaryDocument]] = {}
    seen_by_item: dict[Any, set[Any]] = {}
    for doc in docs_qs:
        targets: set[Any] = set()
        if doc.agenda_item_id in item_ids:
            targets.add(doc.agenda_item_id)
        if doc.paper_id and doc.share_across_committees:
            targets.update(items_by_paper.get(doc.paper_id, []))
        for target_id in targets:
            seen = seen_by_item.setdefault(target_id, set())
            if doc.id not in seen:
                seen.add(doc.id)
                docs_by_item.setdefault(target_id, []).append(doc)
    return docs_by_item


def _file_annotation_counts(organization: Organization, papers_by_item: dict[Any, list[OParlPaper]]) -> dict[Any, int]:
    """Anmerkungs-Zähler für RIS-Dateien (org-weit, wie Fraktionskommentare)."""
    all_file_ids = {f.id for papers in papers_by_item.values() for p in papers for f in p.files.all()}
    if not all_file_ids:
        return {}
    return {
        row["oparl_file"]: row["c"]
        for row in FileAnnotation.objects.filter(organization=organization, oparl_file_id__in=all_file_ids)
        .values("oparl_file")
        .annotate(c=Count("id"))
    }


def resolve_consultations(paper_ids: set[Any], current_meeting: OParlMeeting) -> dict[Any, list[dict[str, Any]]]:
    """Löst je Vorlage die konkrete Beratungsfolge auf: welches Gremium berät wann (mit Sitzungs-Link)."""
    if not paper_ids:
        return {}

    consults = list(OParlConsultation.objects.filter(paper_id__in=paper_ids))
    meeting_ext_ids = {c.meeting_external_id for c in consults if c.meeting_external_id}
    meetings_by_ext = {
        m.external_id: m
        for m in OParlMeeting.objects.filter(external_id__in=meeting_ext_ids).prefetch_related("organizations")
    }

    consultations_by_paper: dict[Any, list[dict[str, Any]]] = {}
    for c in consults:
        m = meetings_by_ext.get(c.meeting_external_id) if c.meeting_external_id else None
        org_names = ", ".join(o.short_name or o.name or "" for o in m.organizations.all()) if m else ""
        consultations_by_paper.setdefault(c.paper_id, []).append(
            {
                "role": c.role or "",
                "authoritative": c.authoritative,
                "meetingId": str(m.id) if m else None,
                "meetingName": (m.name or "") if m else "",
                "meetingStart": timezone.localtime(m.start).strftime("%d.%m.%Y") if m and m.start else "",
                "organization": org_names,
                "isCurrent": bool(m and m.id == current_meeting.id),
                "_sort": m.start.isoformat() if m and m.start else "",
            }
        )

    for entries in consultations_by_paper.values():
        entries.sort(key=lambda e: str(e["_sort"]))
        for e in entries:
            e.pop("_sort", None)

    return consultations_by_paper


def load_preparation_data(organization: Organization, membership: Membership, meeting: OParlMeeting) -> PreparationData:
    """Alle Sektionen aller TOPs einer Sitzung in wenigen Queries laden."""
    agenda_items = sorted_agenda_items(meeting)
    papers_by_item = prefetch_papers_for_agenda_items(agenda_items)

    positions_by_item = {
        pos.agenda_item_id: pos
        for pos in AgendaItemPosition.objects.filter(
            organization=organization, agenda_item__in=agenda_items
        ).select_related("agenda_item", "set_by", "set_by__user")
    }
    private_notes_by_item = {
        note.agenda_item_id: note
        for note in AgendaPrivateNote.objects.filter(author=membership, agenda_item__in=agenda_items)
    }

    own_speeches_by_item: dict[Any, AgendaSpeechNote] = {}
    shared_speeches_by_item: dict[Any, list[AgendaSpeechNote]] = {}
    for sn in AgendaSpeechNote.objects.filter(organization=organization, agenda_item__in=agenda_items).select_related(
        "author", "author__user", "linked_document"
    ):
        if sn.author == membership:
            own_speeches_by_item[sn.agenda_item_id] = sn
        elif sn.is_shared:
            shared_speeches_by_item.setdefault(sn.agenda_item_id, []).append(sn)

    # Org-weite Diskussionsnotizen (nach PaperComment migrierte ausschließen,
    # sonst Doppelanzeige mit dem Vorlagen-Thread) plus Consulting-Notizen
    notes_by_item: dict[Any, list[AgendaItemNote]] = {}
    for note in AgendaItemNote.objects.filter(
        organization=organization, agenda_item__in=agenda_items, migrated_to_paper_comment__isnull=True
    ).select_related("author", "author__user"):
        notes_by_item.setdefault(note.agenda_item_id, []).append(note)
    for item_id, foreign in consulting_notes(organization, agenda_items).items():
        notes_by_item.setdefault(item_id, []).extend(foreign)

    docs_by_item = _documents_by_item(organization, agenda_items, papers_by_item)

    prepared_items: list[PreparedItem] = []
    for item in agenda_items:
        papers = papers_by_item.get(item.id, [])
        prepared_items.append(
            PreparedItem(
                item=item,
                position=positions_by_item.get(item.id),
                private_note=private_notes_by_item.get(item.id),
                own_speech=own_speeches_by_item.get(item.id),
                shared_speeches=shared_speeches_by_item.get(item.id, []),
                notes=notes_by_item.get(item.id, []),
                documents=docs_by_item.get(item.id, []),
                papers=papers,
                primary_paper=papers[0] if papers else None,
                has_files=any(p.files.exists() for p in papers) if papers else False,
            )
        )

    paper_ids = {i.primary_paper.id for i in prepared_items if i.primary_paper}
    return PreparationData(
        agenda_items=agenda_items,
        prepared_items=prepared_items,
        cross_positions=cross_positions(organization, agenda_items),
        file_annotation_counts=_file_annotation_counts(organization, papers_by_item),
        consultations_by_paper=resolve_consultations(paper_ids, meeting),
    )
