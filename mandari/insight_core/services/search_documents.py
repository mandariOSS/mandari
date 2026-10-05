# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Elasticsearch Document Builders.

Converts Django OParl model instances to Elasticsearch document dicts.
Single source of truth for both signal-based indexing and bulk reindex.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Container, Iterable
from typing import Any

#: Textvorschau eines Vorgangs: höchstens so viele Zeichen je Datei und insgesamt
PREVIEW_CHARS_PER_FILE = 5000
PREVIEW_CHARS_TOTAL = 25000


def paper_to_doc(paper, files=None) -> dict[str, Any]:
    """Convert an OParlPaper to a Elasticsearch document.

    Args:
        paper: OParlPaper instance.
        files: Optional queryset/list of related OParlFile objects with text_content.
               If None, auto-fetches from DB via paper.files.
    """
    # Build file contents preview for semantic search / paper-boosting
    if files is None:
        try:
            files = paper.files.filter(
                deleted=False,
                source_missing_since__isnull=True,
                text_content__isnull=False,
                text_extraction_status="completed",
            )
        except Exception:
            files = []

    file_names: list[str] = []
    file_texts: list[str] = []
    total_len = 0
    max_per_file = PREVIEW_CHARS_PER_FILE
    max_total = PREVIEW_CHARS_TOTAL

    for f in files:
        if f.file_name:
            file_names.append(f.file_name)
        if f.text_content and total_len < max_total:
            chunk = f.text_content[:max_per_file].strip()
            file_texts.append(chunk)
            total_len += len(chunk)

    file_contents_preview = "\n\n".join(file_texts)
    if len(file_contents_preview) > max_total:
        file_contents_preview = file_contents_preview[:max_total]

    # Gremien über Beratungen (Consultations) auflösen — ermöglicht den
    # Ausschuss-Filter in der Suche. Die Consultation-raw_json enthält die
    # OParl-Organization-URLs.
    organization_names: list[str] = []
    try:
        org_refs: set[str] = set()
        for consultation in paper.consultations.all():
            refs = (consultation.raw_json or {}).get("organization", [])
            if isinstance(refs, str):
                refs = [refs]
            org_refs.update(r for r in refs if r)
        if org_refs:
            from insight_core.models import OParlOrganization

            organization_names = list(
                OParlOrganization.objects.filter(external_id__in=org_refs).values_list("name", flat=True).distinct()
            )
    except Exception:
        organization_names = []

    return {
        "id": str(paper.id),
        "type": "paper",
        "body_id": str(paper.body_id) if paper.body_id else None,
        "name": paper.name or "",
        "reference": paper.reference or "",
        "paper_type": paper.paper_type or "",
        "date": paper.date.isoformat() if paper.date else None,
        "oparl_created": paper.oparl_created.isoformat() if paper.oparl_created else None,
        "oparl_modified": paper.oparl_modified.isoformat() if paper.oparl_modified else None,
        "file_contents_preview": file_contents_preview,
        "file_names": file_names,
        "organization_names": organization_names,
    }


def meeting_to_doc(meeting) -> dict[str, Any]:
    """Convert an OParlMeeting to a Elasticsearch document."""
    org_names = []
    with contextlib.suppress(Exception):
        org_names = [org.name for org in meeting.organizations.all() if org.name]

    name = meeting.name or ""
    if not name:
        with contextlib.suppress(Exception):
            name = meeting.get_display_name()

    return {
        "id": str(meeting.id),
        "type": "meeting",
        "body_id": str(meeting.body_id) if meeting.body_id else None,
        "name": name,
        "organization_names": org_names,
        "location_name": meeting.location_name or "",
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "cancelled": meeting.cancelled,
        "oparl_modified": meeting.oparl_modified.isoformat() if meeting.oparl_modified else None,
    }


def person_to_doc(person) -> dict[str, Any]:
    """Convert an OParlPerson to a Elasticsearch document."""
    name = person.name or ""
    if not name:
        with contextlib.suppress(Exception):
            name = person.display_name

    return {
        "id": str(person.id),
        "type": "person",
        "body_id": str(person.body_id) if person.body_id else None,
        "name": name,
        "given_name": person.given_name or "",
        "family_name": person.family_name or "",
        "title": person.title or "",
        "oparl_modified": person.oparl_modified.isoformat() if person.oparl_modified else None,
    }


def organization_to_doc(org) -> dict[str, Any]:
    """Convert an OParlOrganization to a Elasticsearch document."""
    return {
        "id": str(org.id),
        "type": "organization",
        "body_id": str(org.body_id) if org.body_id else None,
        "name": org.name or "",
        "short_name": org.short_name or "",
        "organization_type": org.organization_type or "",
        "classification": org.classification or "",
        "oparl_modified": org.oparl_modified.isoformat() if org.oparl_modified else None,
    }


def file_to_doc(file, context_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert an OParlFile to a Elasticsearch document.

    Args:
        file: OParlFile instance.
        context_info: Kontext aus ``file_contexts`` (Gremien, Sitzung, TOP); ein leeres Dict heißt „ohne
                      Sitzung“. ``None``: Der Kontext wird für diese eine Datei nachgeschlagen.
    """
    text_preview = ""
    if file.text_content:
        text_preview = file.text_content[:500].strip()
        if len(file.text_content) > 500:
            text_preview += "..."

    doc = {
        "id": str(file.id),
        "type": "file",
        "body_id": str(file.body_id) if file.body_id else None,
        "name": file.name or "",
        "file_name": file.file_name or "",
        "mime_type": file.mime_type or "",
        "access_url": file.access_url or "",
        "text_content": file.text_content or "",
        "text_preview": text_preview,
        "paper_id": str(file.paper_id) if file.paper_id else None,
        "paper_name": file.paper.name if file.paper else None,
        "paper_reference": file.paper.reference if file.paper else None,
        "meeting_id": str(file.meeting_id) if file.meeting_id else None,
        "oparl_modified": file.oparl_modified.isoformat() if file.oparl_modified else None,
        # Context fields (Gremium, Sitzung, TOP)
        "organization_names": [],
        "meeting_name": None,
        "meeting_date": None,
        "agenda_number": None,
    }

    if context_info is None:
        # Einzelne Datei (Signal, reindex_elasticsearch): Kontext hier nachschlagen
        context_info = _single_file_context(file)
    if context_info:
        doc["organization_names"] = context_info.get("organization_names", [])
        doc["meeting_name"] = context_info.get("meeting_name")
        doc["meeting_date"] = context_info.get("meeting_date")
        doc["agenda_number"] = context_info.get("agenda_number")

    return doc


def _single_file_context(file: Any) -> dict[str, Any]:
    """Kontext einer einzelnen Datei; ein Fehler darf das Speichern (Signal) nicht verhindern."""
    try:
        return file_contexts([file]).get(file.pk, {})
    except Exception:
        return {}


_ZIFFERN = re.compile(r"([0-9]+)")


def natural_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """Natürliche Sortierung: Zahlen als Zahlen, ``…/consultation/9`` vor ``…/consultation/10``."""
    return tuple((0, int(teil)) if index % 2 else (1, teil) for index, teil in enumerate(_ZIFFERN.split(text)) if teil)


def _consultation_rank(consultation: Any, sitzungen: Container[str]) -> tuple[bool, bool, tuple[Any, ...]]:
    """
    Reihenfolge der Beratungen eines Vorgangs, unabhängig vom Zeitpunkt: zuerst die federführende
    (``authoritative``), dann eine mit Sitzung im Bestand (``sitzungen``: Kennungen der Sitzungen, die es gibt
    und die nicht zurückgenommen sind), dann nach Kennung der Quelle, natürlich sortiert.
    """
    hat_sitzung = bool(consultation.meeting_external_id) and consultation.meeting_external_id in sitzungen
    return (not consultation.authoritative, not hat_sitzung, natural_key(consultation.external_id or ""))


def file_contexts(files: Iterable[Any]) -> dict[Any, dict[str, Any]]:
    """
    Kontext je Datei (Gremien, Sitzung, Tagesordnungspunkt), gebündelt für beliebig viele Dateien.

    Höchstens fünf Abfragen: Beratungen, vorhandene Sitzungen der Beratungen, gewählte Sitzungen mit ihren
    Gremien, Tagesordnungspunkte. Dazu kommt nur für eine Sitzung ohne benannte Gremien, deren Gremien die
    Quelle nennt, die Namensauflösung von ``get_display_name``. Die Dateien brauchen ``pk``, ``paper_id``
    und ``meeting_id``.

    Kette: Datei → Vorgang → Beratung → Sitzung (→ Gremien) und Tagesordnungspunkt; ohne Sitzung über die
    Beratung gilt die Sitzung, an der die Datei direkt hängt. Je Vorgang zählt eine Beratung nach
    ``_consultation_rank``: federführend, mit Sitzung im Bestand, kleinste Kennung (natürlich sortiert).
    Von mandari Session zurückgenommene Beratungen, Sitzungen und Tagesordnungspunkte bleiben außen vor,
    wie in der Dokumentliste (``withdrawn_q``).

    Rückgabe: Datei (``pk``) → ``{organization_names, meeting_name, meeting_date, agenda_number}``; leer,
    wenn sich keine Sitzung findet.
    """
    from django.db.models import Q

    from insight_core.models import OParlAgendaItem, OParlConsultation, OParlMeeting, withdrawn_q

    dateien = list(files)
    vorgaenge = {datei.paper_id for datei in dateien if datei.paper_id}
    beratung_je_vorgang: dict[Any, Any] = {}
    if vorgaenge:
        beratungen = list(
            OParlConsultation.objects.filter(paper_id__in=vorgaenge)
            .exclude(withdrawn_q())
            .only("pk", "paper_id", "external_id", "authoritative", "meeting_external_id", "agenda_item_external_id")
        )
        verweise = {b.meeting_external_id for b in beratungen if b.meeting_external_id}
        vorhanden: set[str] = set()
        if verweise:
            vorhanden = set(
                OParlMeeting.objects.filter(external_id__in=verweise)
                .exclude(withdrawn_q())
                .values_list("external_id", flat=True)
            )
        for kandidat in beratungen:
            bisher = beratung_je_vorgang.get(kandidat.paper_id)
            if bisher is None or _consultation_rank(kandidat, vorhanden) < _consultation_rank(bisher, vorhanden):
                beratung_je_vorgang[kandidat.paper_id] = kandidat

    mit_sitzung = [b for b in beratung_je_vorgang.values() if b.meeting_external_id]
    sitzungen_ext = {b.meeting_external_id for b in mit_sitzung}
    sitzungen_pk = {datei.meeting_id for datei in dateien if datei.meeting_id}
    sitzung_je_ext: dict[str, Any] = {}
    sitzung_je_pk: dict[Any, Any] = {}
    if sitzungen_ext or sitzungen_pk:
        for geladen in (
            OParlMeeting.objects.filter(Q(external_id__in=sitzungen_ext) | Q(pk__in=sitzungen_pk))
            .exclude(withdrawn_q())
            .prefetch_related("organizations")
        ):
            sitzung_je_ext[geladen.external_id] = geladen
            sitzung_je_pk[geladen.pk] = geladen

    punkte_ext = {b.agenda_item_external_id for b in mit_sitzung if b.agenda_item_external_id}
    nummer_je_punkt: dict[str, Any] = {}
    if punkte_ext:
        nummer_je_punkt = dict(
            OParlAgendaItem.objects.filter(external_id__in=punkte_ext)
            .exclude(withdrawn_q())
            .values_list("external_id", "number")
        )

    kontexte: dict[Any, dict[str, Any]] = {}
    for datei in dateien:
        sitzung: Any = None
        nummer: str | None = None
        beratung: Any = beratung_je_vorgang.get(datei.paper_id) if datei.paper_id else None
        if beratung is not None and beratung.meeting_external_id:
            sitzung = sitzung_je_ext.get(beratung.meeting_external_id)
            if beratung.agenda_item_external_id:
                nummer = nummer_je_punkt.get(beratung.agenda_item_external_id)
        if sitzung is None and datei.meeting_id:
            sitzung = sitzung_je_pk.get(datei.meeting_id)
        if sitzung is None:
            kontexte[datei.pk] = {}
            continue
        kontexte[datei.pk] = {
            "organization_names": [org.name for org in sitzung.organizations.all() if org.name],
            "meeting_name": sitzung.get_display_name(),
            "meeting_date": sitzung.start.isoformat() if sitzung.start else None,
            "agenda_number": nummer,
        }
    return kontexte
