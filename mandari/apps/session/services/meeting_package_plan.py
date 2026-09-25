# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsmappe (Issue #218): Welche Unterlagen gehören in welche Fassung?

Der Plan ist die gemeinsame Grundlage für Gesamt-PDF, ZIP-Paket und Fingerabdruck. Er
liest nur Datenbankzeilen, keine Dateien – so bleibt die Anforderung einer Mappe im
Seitenaufruf billig; die Dateien öffnet erst die Erzeugung im Hintergrund.

Sichtbarkeit exakt wie in der Oberfläche, ohne eigene Parallellogik:
- Tagesordnung über ``agenda_service.grouped_agenda`` (öffentliche Fassung: nur Ö-TOPs
  und Ö-Unterpunkte),
- Vorlagen nach der Regel der Vorlagen-Detailseite (NÖ-Vorlage nur mit
  ``view_non_public_papers``),
- Anlagen über ``file_service.file_visible`` – dieselbe Prüfung wie der Anlagen-Download.

Jede Fassung steht für genau einen Berechtigungssatz (``VARIANT_PERMISSIONS``): Wer ihn
hat, sieht heute schon jeden enthaltenen Bestandteil einzeln.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator, Set
from dataclasses import dataclass, field
from typing import Any, cast

from django.db.models import prefetch_related_objects
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionFile,
    SessionMeeting,
    SessionMeetingPackage,
    SessionPaper,
)
from apps.session.services import agenda_service, file_service

#: Erhöhen, wenn sich Aufbau oder Gestaltung der Mappe ändern – ältere Fassungen gelten dann als veraltet
FORMAT_VERSION = 1

PUBLIC = SessionMeetingPackage.VARIANT_PUBLIC
INTERNAL = SessionMeetingPackage.VARIANT_INTERNAL
VARIANT_LABELS: dict[str, str] = dict(SessionMeetingPackage.VARIANT_CHOICES)

#: Berechtigungen, die eine Fassung voraussetzt. Die Mappe enthält Tagesordnung (Sitzung),
#: Vorlagen und deren Anlagen – also braucht sie die Sichtrechte beider Bereiche.
VARIANT_PERMISSIONS: dict[str, frozenset[str]] = {
    PUBLIC: frozenset({"view_meetings", "view_papers"}),
    INTERNAL: frozenset(
        {"view_meetings", "view_papers", "view_non_public_meetings", "view_non_public_papers"},
    ),
}

AGENDA_ZIP_PATH = "Einladung und Tagesordnung.pdf"
MEETING_FILES_FOLDER = "Weitere Unterlagen"


def variants_for(permissions: Set[str], meeting: SessionMeeting) -> list[str]:
    """Fassungen, die ein Nutzer mit diesen Berechtigungen abrufen darf (öffentliche zuerst).

    Eine nichtöffentliche Sitzung hat keine öffentliche Fassung.
    """
    variants: list[str] = []
    if meeting.is_public and VARIANT_PERMISSIONS[PUBLIC] <= permissions:
        variants.append(PUBLIC)
    if VARIANT_PERMISSIONS[INTERNAL] <= permissions:
        variants.append(INTERNAL)
    return variants


# =============================================================================
# Planstruktur
# =============================================================================


@dataclass
class PlannedFile:
    """Eine Anlage samt Pfad im ZIP-Paket."""

    file: SessionFile
    zip_path: str = ""


@dataclass
class PlannedPaper:
    """Eine Vorlage mit ihren sichtbaren Anlagen."""

    paper: SessionPaper
    files: list[PlannedFile]
    folder: str = ""
    document_zip_path: str = ""


@dataclass
class PlannedTop:
    """Ein Tagesordnungspunkt mit Vorlage, TOP-Anlagen und Unterpunkten."""

    item: SessionAgendaItem
    paper: PlannedPaper | None
    files: list[PlannedFile]
    children: list[PlannedTop] = field(default_factory=list)
    folder: str = ""

    @property
    def has_documents(self) -> bool:
        return self.paper is not None or bool(self.files)


@dataclass
class PlannedSection:
    """Öffentlicher bzw. nichtöffentlicher Teil der Tagesordnung."""

    title: str
    non_public: bool
    tops: list[PlannedTop]


@dataclass
class MeetingPackagePlan:
    """Inhalt einer Fassung: Abschnitte mit TOPs, Sitzungsanlagen, abgeleitete Sitzungsnummer."""

    meeting: SessionMeeting
    variant: str
    sections: list[PlannedSection]
    meeting_files: list[PlannedFile]
    ordinal: str

    @property
    def variant_label(self) -> str:
        return VARIANT_LABELS[self.variant]

    @property
    def is_internal(self) -> bool:
        return self.variant == INTERNAL

    def all_tops(self) -> Iterator[PlannedTop]:
        """Alle TOPs in Dokumentreihenfolge, Unterpunkte direkt nach ihrem TOP."""

        def walk(tops: list[PlannedTop]) -> Iterator[PlannedTop]:
            for top in tops:
                yield top
                yield from walk(top.children)

        for section in self.sections:
            yield from walk(section.tops)

    def all_papers(self) -> Iterator[PlannedPaper]:
        for top in self.all_tops():
            if top.paper is not None:
                yield top.paper

    def all_files(self) -> Iterator[PlannedFile]:
        for top in self.all_tops():
            if top.paper is not None:
                yield from top.paper.files
            yield from top.files
        yield from self.meeting_files

    def contents(self) -> dict[str, Any]:
        """Enthaltene Objekte – Grundlage der Sperre älterer Fassungen (siehe Service)."""
        return {
            "meeting_public": self.meeting.is_public,
            "items": sorted({str(top.item.pk) for top in self.all_tops()}),
            "papers": sorted({str(paper.paper.pk) for paper in self.all_papers()}),
            "files": sorted({str(planned.file.pk) for planned in self.all_files()}),
        }

    def fingerprint(self) -> str:
        """SHA-256 über alles, was in PDF und ZIP sichtbar wird."""
        meeting = self.meeting
        tenant = meeting.tenant
        payload = {
            "format": FORMAT_VERSION,
            "variant": self.variant,
            "tenant": [
                tenant.name,
                tenant.address,
                tenant.contact_email,
                tenant.contact_phone,
                tenant.primary_color,
                tenant.reference_label,
            ],
            "meeting": [
                meeting.name,
                meeting.organization.name,
                meeting.start,
                meeting.end,
                meeting.location,
                meeting.room,
                meeting.street_address,
                meeting.postal_code,
                meeting.locality,
                meeting.invitation_sent_at,
                meeting.cancelled,
                meeting.is_public,
                self.ordinal,
            ],
            "sections": [[s.title, [_top_fingerprint(t) for t in s.tops]] for s in self.sections],
            "meeting_files": [_file_fingerprint(f.file) for f in self.meeting_files],
        }
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _file_fingerprint(session_file: SessionFile) -> list[Any]:
    return [
        str(session_file.pk),
        session_file.name,
        session_file.version,
        session_file.size,
        session_file.mime_type,
        session_file.is_public,
        session_file.updated_at,
    ]


def _paper_fingerprint(planned: PlannedPaper) -> list[Any]:
    paper = planned.paper
    return [
        str(paper.pk),
        paper.reference,
        paper.name,
        paper.paper_type,
        paper.date,
        paper.is_public,
        _digest(paper.main_text),
        _digest(paper.resolution_text),
        paper.has_financial_impact,
        paper.financial_impact_note,
        paper.lead_department.name if paper.lead_department else "",
        [_file_fingerprint(f.file) for f in planned.files],
    ]


def _top_fingerprint(top: PlannedTop) -> list[Any]:
    item = top.item
    return [
        str(item.pk),
        item.number,
        item.name,
        item.is_public,
        item.is_withdrawn,
        item.is_supplementary,
        _paper_fingerprint(top.paper) if top.paper else None,
        [_file_fingerprint(f.file) for f in top.files],
        [_top_fingerprint(child) for child in top.children],
    ]


# =============================================================================
# Plan aufbauen
# =============================================================================


def meeting_ordinal(meeting: SessionMeeting) -> str:
    """
    Laufende Sitzungsnummer des Gremiums, abgeleitet aus den erfassten Sitzungen.

    Gezählt werden nicht abgesagte Sitzungen desselben Gremiums bis zu dieser – je
    Wahlperiode, ohne Wahlperiode je Kalenderjahr. Abgesagte Sitzungen erhalten keine Nummer.
    """
    if meeting.cancelled:
        return ""
    qs = SessionMeeting.objects.filter(
        tenant_id=meeting.tenant_id,
        organization_id=meeting.organization_id,
        cancelled=False,
        start__lte=meeting.start,
    )
    if meeting.legislative_term_id:
        qs = qs.filter(legislative_term_id=meeting.legislative_term_id)
        scope = meeting.legislative_term.name if meeting.legislative_term else ""
    else:
        year = timezone.localtime(meeting.start).year
        qs = qs.filter(start__year=year)
        scope = f"Jahr {year}"
    number = qs.count()
    return f"{number}. Sitzung ({scope})" if scope else f"{number}. Sitzung"


def _children(item: SessionAgendaItem) -> list[SessionAgendaItem]:
    """Von ``grouped_agenda`` vorgeladene (bereits nach Ö/NÖ gefilterte) Unterpunkte."""
    return cast(list[SessionAgendaItem], getattr(item, "children_list", []))


def build_plan(meeting: SessionMeeting, variant: str) -> MeetingPackagePlan:
    """Inhalt der Fassung ``variant`` ermitteln (ohne Dateien zu öffnen)."""
    if variant not in VARIANT_PERMISSIONS:
        raise ValueError(f"Unbekannte Fassung: {variant}")
    permissions = VARIANT_PERMISSIONS[variant]
    include_non_public = variant == INTERNAL
    tenant_id = meeting.tenant_id

    agenda = agenda_service.grouped_agenda(meeting, include_non_public=include_non_public)
    top_level: list[SessionAgendaItem] = list(agenda["public"]) + list(agenda["non_public"])
    items: list[SessionAgendaItem] = []
    for item in top_level:
        items.append(item)
        items.extend(_children(item))
    active = [item for item in items if not item.is_withdrawn]

    # Vorlagen: Regel der Vorlagen-Detailseite (NÖ-Vorlage nur mit view_non_public_papers)
    papers: dict[Any, SessionPaper] = {}
    for item in active:
        paper = item.paper
        if paper is None or paper.tenant_id != tenant_id:
            continue
        if paper.is_public or "view_non_public_papers" in permissions:
            papers[paper.pk] = paper
    prefetch_related_objects(list(papers.values()), "lead_department")

    paper_files: dict[Any, list[SessionFile]] = {}
    for session_file in (
        SessionFile.objects.filter(tenant_id=tenant_id, paper_id__in=list(papers))
        .select_related("paper")
        .order_by("name", "created_at")
    ):
        if file_service.file_visible(permissions, session_file):
            paper_files.setdefault(session_file.paper_id, []).append(session_file)

    item_files: dict[Any, list[SessionFile]] = {}
    for session_file in (
        SessionFile.objects.filter(
            tenant_id=tenant_id, agenda_item_id__in=[item.pk for item in active], paper__isnull=True
        )
        .select_related("agenda_item__meeting")
        .order_by("name", "created_at")
    ):
        if file_service.file_visible(permissions, session_file):
            item_files.setdefault(session_file.agenda_item_id, []).append(session_file)

    meeting_files = [
        PlannedFile(session_file)
        for session_file in SessionFile.objects.filter(
            tenant_id=tenant_id, meeting=meeting, paper__isnull=True, agenda_item__isnull=True
        )
        .select_related("meeting")
        .order_by("name", "created_at")
        if file_service.file_visible(permissions, session_file)
    ]

    def plan_top(item: SessionAgendaItem) -> PlannedTop:
        planned_paper = None
        files: list[PlannedFile] = []
        if not item.is_withdrawn:
            if item.paper_id in papers:
                paper = papers[item.paper_id]
                planned_paper = PlannedPaper(paper, [PlannedFile(f) for f in paper_files.get(paper.pk, [])])
            files = [PlannedFile(f) for f in item_files.get(item.pk, [])]
        children = [plan_top(child) for child in _children(item)]
        return PlannedTop(item, planned_paper, files, children)

    sections = [PlannedSection("Öffentlicher Teil", False, [plan_top(item) for item in agenda["public"]])]
    if include_non_public and agenda["non_public"]:
        sections.append(PlannedSection("Nichtöffentlicher Teil", True, [plan_top(i) for i in agenda["non_public"]]))

    plan = MeetingPackagePlan(
        meeting=meeting,
        variant=variant,
        sections=sections,
        meeting_files=meeting_files,
        ordinal=meeting_ordinal(meeting),
    )
    _assign_zip_paths(plan)
    return plan


# =============================================================================
# Ordnerstruktur im ZIP-Paket
# =============================================================================

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
_SEPARATORS = re.compile(r"[/\\]")
_WHITESPACE = re.compile(r"\s+")
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
MAX_COMPONENT_LENGTH = 80


def safe_component(name: str, fallback: str = "Unbenannt") -> str:
    """
    Einen Pfadbestandteil für das ZIP-Paket bereinigen.

    Umlaute und Sonderzeichen bleiben erhalten (NFC, UTF-8-Flag setzt ``zipfile``);
    entfernt werden Pfadtrenner, Steuer- und unter Windows verbotene Zeichen, führende und
    abschließende Punkte/Leerzeichen („..“ ergibt nie einen Pfadsprung) und reservierte
    Gerätenamen. Lange Namen werden gekürzt, die Dateiendung bleibt.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = _SEPARATORS.sub("-", _WHITESPACE.sub(" ", text))
    text = _UNSAFE_CHARS.sub("_", text).strip(" .")
    if not text:
        text = fallback
    stem, dot, ext = text.rpartition(".")
    if not dot or not stem or len(ext) > 10 or " " in ext:
        stem, ext = text, ""
    else:
        ext = "." + ext
    if stem.split(".")[0].upper() in _RESERVED_NAMES:
        stem = "_" + stem
    stem = stem[: max(1, MAX_COMPONENT_LENGTH - len(ext))].rstrip(" .") or fallback
    return stem + ext


def _unique(path: str, used: set[str]) -> str:
    """Pfad eindeutig machen (ohne Rücksicht auf Groß-/Kleinschreibung, wie unter Windows)."""
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    else:
        ext = "." + ext
    candidate = path
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{folder}/{stem} ({counter}){ext}" if folder else f"{stem} ({counter}){ext}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _join(*parts: str) -> str:
    return "/".join(part for part in parts if part)


def _assign_zip_paths(plan: MeetingPackagePlan) -> None:
    """Ordnerstruktur ``<TOP-Nr> <TOP-Titel>/<Aktenzeichen der Vorlage>/<Anlagenname>``."""
    used: set[str] = {AGENDA_ZIP_PATH.casefold()}

    def assign(top: PlannedTop, base: str) -> None:
        item = top.item
        top.folder = _join(base, safe_component(f"{item.number} {item.name}", fallback=f"TOP {item.number}"))
        if top.paper is not None:
            paper = top.paper.paper
            reference = paper.reference or "Vorlage ohne Nummer"
            top.paper.folder = _join(top.folder, safe_component(reference, fallback="Vorlage"))
            top.paper.document_zip_path = _unique(
                _join(top.paper.folder, safe_component(f"Vorlage {reference}.pdf", fallback="Vorlage.pdf")), used
            )
            for planned in top.paper.files:
                planned.zip_path = _unique(_join(top.paper.folder, safe_component(planned.file.name)), used)
        for planned in top.files:
            planned.zip_path = _unique(_join(top.folder, safe_component(planned.file.name)), used)
        for child in top.children:
            assign(child, top.folder)

    for section in plan.sections:
        for top in section.tops:
            assign(top, "")
    for planned in plan.meeting_files:
        planned.zip_path = _unique(_join(MEETING_FILES_FOLDER, safe_component(planned.file.name)), used)
