# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fassungen von Vorlagen (Issue #226): sichern, anzeigen und wiederherstellen.

Wann entsteht eine Fassung?
- Bei jedem Workflow-Übergang: Der Signal-Hook in ``signals.py`` ruft ``record_transition`` bei
  jedem Statuswechsel der Vorlage – ob Freigabelauf, Mitzeichnung, Terminierung oder
  Bearbeiten-Formular. Der Workflow-Code selbst bleibt unberührt.
- Bei jedem erfassten Beratungsergebnis (``record_consultation``). „Angenommen“ in der
  entscheidenden Beratung kennzeichnet die beschlossene Fassung – genau eine je Vorlage.
- Von Hand („Fassung sichern“, ``snapshot``).

Eine Fassung hält Texte, Angaben und die Anlagen mit ihrem Inhalt fest (``SessionPaperVersionFile``
verweist auf den gespeicherten Inhalt, nicht auf die Anlage). Wiederherstellen legt eine neue
Fassung an und ist nur im Entwurf möglich (``RESTORE_STATUSES``).

Sichtbarkeit (``version_visible``, ``entry_visible``): nie weiter als die Vorlage bzw. Anlage heute
und nie weiter als zum Zeitpunkt der Fassung. Anlagen, die inzwischen gelöscht sind, sehen nur
Berechtigte für nichtöffentliche Vorlagen.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Set
from dataclasses import dataclass, field
from typing import Any, cast

from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionFileVersion,
    SessionOrganization,
    SessionPaper,
    SessionPaperVersion,
    SessionPaperVersionFile,
    SessionPerson,
    SessionUser,
)
from apps.session.services import file_service, file_version_service

logger = logging.getLogger(__name__)

_log_event = cast(Any, audit).log_event
_get_current_request = cast(Any, audit).get_current_request

Version = SessionPaperVersion
Entry = SessionPaperVersionFile

#: Nur im Entwurf lässt sich eine Fassung wiederherstellen. Nach der Freigabe ändert sich eine
#: Vorlage über eine Neufassung (Unternummer) oder nach Zurückweisung wieder im Entwurf.
RESTORE_STATUSES = frozenset({"draft"})

#: Felder, die „Wiederherstellen“ aus der Fassung übernimmt. Nicht dabei: Nummer (unveränderlich),
#: Status (Workflow) und Ö/NÖ (Sichtbarkeitsentscheidung, keine Inhaltsfrage).
RESTORED_FIELDS = (
    "name",
    "paper_type",
    "date",
    "deadline",
    "main_text",
    "resolution_text",
    "has_financial_impact",
    "financial_impact_note",
)
#: Verknüpfungen in ``details`` (Schlüssel -> Modell), ebenfalls wiederherstellbar
RESTORED_RELATIONS: dict[str, type[SessionOrganization] | type[SessionPerson]] = {
    "main_organization": SessionOrganization,
    "lead_department": SessionOrganization,
    "originator_organization": SessionOrganization,
    "originator_person": SessionPerson,
}

#: Beratungsergebnis, das eine Vorlage beschließt
RESOLVED_RESULT = "approved"


class RestoreRefusedError(Exception):
    """Wiederherstellen nicht möglich (nutzerfreundliche Meldung)."""


# =============================================================================
# Stand einer Vorlage erfassen
# =============================================================================


def _named(obj: Any, name: str) -> dict[str, str] | None:
    return {"id": str(obj.pk), "name": name} if obj is not None else None


def _details(paper: SessionPaper) -> dict[str, Any]:
    person = paper.originator_person
    parent = paper.parent_paper
    return {
        "main_organization": _named(paper.main_organization, getattr(paper.main_organization, "name", "")),
        "lead_department": _named(paper.lead_department, getattr(paper.lead_department, "name", "")),
        "originator_organization": _named(
            paper.originator_organization, getattr(paper.originator_organization, "name", "")
        ),
        "originator_person": _named(person, person.display_name if person is not None else ""),
        "parent": (
            {"reference": parent.reference, "relation": paper.get_relation_type_display()}
            if parent is not None
            else None
        ),
    }


def _fields(paper: SessionPaper) -> dict[str, Any]:
    return {
        "name": paper.name,
        "reference": paper.reference,
        "paper_type": paper.paper_type,
        "is_public": paper.is_public,
        "status": paper.status,
        "date": paper.date,
        "deadline": paper.deadline,
        "main_text": paper.main_text,
        "resolution_text": paper.resolution_text,
        "has_financial_impact": paper.has_financial_impact,
        "financial_impact_note": paper.financial_impact_note,
        "details": _details(paper),
    }


def _entries(paper: SessionPaper) -> list[dict[str, Any]]:
    """Anlagen der Vorlage mit ihrem aktuellen Inhalt (Bestand wird dabei nachträglich erfasst)."""
    files = paper.files.order_by("created_at", "name").prefetch_related(
        Prefetch("versions", queryset=SessionFileVersion.objects.select_related("blob").order_by("-number"))
    )
    entries = []
    for position, session_file in enumerate(files):
        current = file_version_service.ensure_current_version(session_file, list(session_file.versions.all()))
        blob = current.blob if current is not None else None
        entries.append(
            {
                "attachment_id": session_file.pk,
                "file_version_number": session_file.version,
                "blob": blob,
                "name": session_file.name,
                "mime_type": session_file.mime_type,
                "size": blob.size if blob is not None else session_file.size,
                "sha256": blob.sha256 if blob is not None else "",
                "is_public": session_file.is_public,
                "position": position,
            }
        )
    return entries


def _fingerprint(fields: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    payload = {
        "fields": {key: value for key, value in fields.items() if key != "status"},
        "files": [[str(e["attachment_id"]), e["sha256"], e["name"], e["is_public"]] for e in entries],
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def current_fingerprint(paper: SessionPaper) -> str:
    """Fingerabdruck des aktuellen Stands (Texte, Angaben, Anlagen – ohne Status)."""
    return _fingerprint(_fields(paper), _entries(paper))


def snapshot(
    paper: SessionPaper,
    *,
    trigger: str,
    user: SessionUser | None = None,
    note: str = "",
    previous_status: str = "",
    agenda_item: SessionAgendaItem | None = None,
    restored_from: SessionPaperVersion | None = None,
    is_resolved: bool = False,
) -> SessionPaperVersion:
    """Aktuellen Stand der Vorlage als neue, unveränderliche Fassung sichern."""
    with transaction.atomic():
        # Nummern je Vorlage fortlaufend: Zeile der Vorlage sperren, bis die Fassung steht
        list(SessionPaper.objects.select_for_update().filter(pk=paper.pk).values_list("pk", flat=True))
        number = (Version.objects.filter(paper=paper).aggregate(m=Max("number"))["m"] or 0) + 1
        fields = _fields(paper)
        entries = _entries(paper)
        version = Version.objects.create(
            tenant_id=paper.tenant_id,
            paper=paper,
            number=number,
            trigger=trigger,
            note=note[:300],
            previous_status=previous_status,
            is_resolved=is_resolved,
            agenda_item=agenda_item,
            restored_from=restored_from,
            fingerprint=_fingerprint(fields, entries),
            created_by=user,
            **fields,
        )
        Entry.objects.bulk_create([Entry(version=version, **entry) for entry in entries])
    return version


def _actor(tenant_id: Any) -> SessionUser | None:
    """Auslösende Person aus dem laufenden Request (wie das Audit-Log); None bei Hintergrundläufen."""
    request = _get_current_request()
    session_user = getattr(request, "session_user", None) if request is not None else None
    if session_user is not None and session_user.tenant_id == tenant_id:
        return cast(SessionUser, session_user)
    return None


def _safely[T](action: Callable[[], T]) -> T | None:
    """
    Automatische Fassungen dürfen den Workflow nie aufhalten: Scheitert das Sichern (etwa ein
    Speicherfehler), läuft der Übergang weiter und der Fehler landet im Log.
    """
    try:
        with transaction.atomic():
            return action()
    except Exception:
        logger.exception("Fassung der Vorlage konnte nicht gesichert werden.")
        return None


def _status_label(status: str) -> str:
    return str(dict(SessionPaper._meta.get_field("status").choices or []).get(status, status))


def record_transition(paper: SessionPaper, previous_status: str) -> SessionPaperVersion | None:
    """Fassung bei einem Workflow-Übergang (Statuswechsel) – aufgerufen vom Signal-Hook."""
    note = f"{_status_label(previous_status)} → {_status_label(paper.status)}"
    return _safely(
        lambda: snapshot(
            paper,
            trigger=Version.TRIGGER_TRANSITION,
            user=_actor(paper.tenant_id),
            note=note,
            previous_status=previous_status,
        )
    )


def _decisive(paper: SessionPaper, consultation: SessionConsultation | None) -> bool:
    """Entscheidet diese Beratung abschließend? Ohne Beratungsstation: wenn die Vorlage keine entscheidende hat."""
    if consultation is not None:
        return bool(consultation.authoritative or consultation.role == "decision")
    return not paper.consultations.filter(Q(authoritative=True) | Q(role="decision")).exists()


def record_consultation(
    paper: SessionPaper,
    *,
    result: str,
    result_label: str,
    consultation: SessionConsultation | None = None,
    agenda_item: SessionAgendaItem | None = None,
) -> SessionPaperVersion | None:
    """
    Fassung zu einem erfassten Beratungsergebnis. „Angenommen“ in der entscheidenden Beratung
    kennzeichnet die beschlossene Fassung, sofern die Vorlage noch keine hat.
    """

    def record() -> SessionPaperVersion:
        # Frisch aus der Datenbank: Das Objekt am TOP bzw. an der Station kann älter sein als der gespeicherte Stand
        current = SessionPaper.objects.get(pk=paper.pk)
        parts = [result_label]
        if consultation is not None:
            parts.append(f"{consultation.organization.name} ({consultation.get_role_display()})")
        if agenda_item is not None:
            start = timezone.localtime(agenda_item.meeting.start)
            parts.append(f"Sitzung vom {start:%d.%m.%Y}, TOP {agenda_item.number}")
        resolved = (
            result == RESOLVED_RESULT
            and _decisive(current, consultation)
            and not Version.objects.filter(paper=current, is_resolved=True).exists()
        )
        return snapshot(
            current,
            trigger=Version.TRIGGER_CONSULTATION,
            user=_actor(current.tenant_id),
            note=" · ".join(parts),
            agenda_item=agenda_item,
            is_resolved=resolved,
        )

    return _safely(record)


# =============================================================================
# Sichtbarkeit
# =============================================================================


def paper_visible(permissions: Set[str], paper: SessionPaper) -> bool:
    """Vorlage sichtbar? Dieselbe Regel wie Liste und Detailseite."""
    if "view_papers" not in permissions:
        return False
    return bool(paper.is_public) or "view_non_public_papers" in permissions


def version_visible(permissions: Set[str], paper: SessionPaper, version: SessionPaperVersion) -> bool:
    """Fassung sichtbar? Wie die Vorlage heute und wie zum Zeitpunkt der Fassung."""
    if not paper_visible(permissions, paper):
        return False
    return bool(version.is_public) or "view_non_public_papers" in permissions


def current_attachments(paper: SessionPaper, entries: list[SessionPaperVersionFile]) -> dict[Any, SessionFile]:
    """Heute noch vorhandene Anlagen der Vorlage zu den Einträgen (Kennung -> Anlage)."""
    ids = {entry.attachment_id for entry in entries}
    if not ids:
        return {}
    files = SessionFile.objects.filter(tenant_id=paper.tenant_id, paper=paper, pk__in=ids).select_related("paper")
    return {session_file.pk: session_file for session_file in files}


def entry_visible(
    permissions: Set[str],
    paper: SessionPaper,
    version: SessionPaperVersion,
    entry: SessionPaperVersionFile,
    attachments: dict[Any, SessionFile],
) -> bool:
    """
    Anlage einer Fassung sichtbar? Wie die Anlage heute (``file_service.file_visible``) und wie
    zum Zeitpunkt der Fassung; ist die Anlage inzwischen gelöscht, nur mit dem NÖ-Recht.
    """
    if not version_visible(permissions, paper, version):
        return False
    non_public = "view_non_public_papers" in permissions
    session_file = attachments.get(entry.attachment_id)
    if session_file is None:
        return non_public
    if not file_service.file_visible(permissions, session_file):
        return False
    return bool(entry.is_public) or non_public


def visible_versions(permissions: Set[str], paper: SessionPaper) -> list[SessionPaperVersion]:
    """Fassungen der Vorlage, die diese Berechtigungen sehen dürfen (neueste zuerst)."""
    if not paper_visible(permissions, paper):
        return []
    qs = Version.objects.filter(paper=paper).select_related("created_by__user", "restored_from")
    if "view_non_public_papers" not in permissions:
        qs = qs.filter(is_public=True)
    return list(qs.order_by("-number"))


@dataclass
class VersionEntries:
    """Sichtbare Anlagen einer Fassung samt der heute noch vorhandenen Anlagen."""

    entries: list[SessionPaperVersionFile]
    attachments: dict[Any, SessionFile] = field(default_factory=dict)


def visible_entries(permissions: Set[str], paper: SessionPaper, version: SessionPaperVersion) -> VersionEntries:
    entries = list(version.files.select_related("blob").order_by("position"))
    attachments = current_attachments(paper, entries)
    return VersionEntries(
        entries=[e for e in entries if entry_visible(permissions, paper, version, e, attachments)],
        attachments=attachments,
    )


def detail_context(paper: SessionPaper, permissions: Set[str]) -> dict[str, Any]:
    """Kurzüberblick für die Seitenleiste der Vorlage: Anzahl, neueste und beschlossene Fassung."""
    qs = Version.objects.filter(paper=paper)
    if "view_non_public_papers" not in permissions:
        qs = qs.filter(is_public=True)
    latest = qs.order_by("-number").first()
    return {
        "version_latest": latest,
        "version_resolved": qs.filter(is_resolved=True).first() if latest is not None else None,
        "version_can_save": "edit_papers" in permissions,
    }


# =============================================================================
# Wiederherstellen
# =============================================================================


def restore_blocker(paper: SessionPaper) -> str | None:
    """Grund, warum sich gerade keine Fassung wiederherstellen lässt; None, wenn es geht."""
    if paper.status not in RESTORE_STATUSES:
        return (
            f"Wiederherstellen ist nur im Entwurf möglich (aktuell: {paper.get_status_display()}). "
            "Nach der Freigabe entsteht eine Änderung als Neufassung (Unternummer)."
        )
    return None


@dataclass
class RestoreResult:
    version: SessionPaperVersion
    notes: list[str]


def _restore_relations(paper: SessionPaper, details: dict[str, Any], notes: list[str]) -> None:
    for key, model in RESTORED_RELATIONS.items():
        value = details.get(key)
        if value is None:
            setattr(paper, key, None)
            continue
        obj = model.objects.filter(tenant_id=paper.tenant_id, pk=value.get("id")).first()
        if obj is None:
            notes.append(f"„{value.get('name', '')}“ gibt es nicht mehr – die bisherige Angabe bleibt.")
            continue
        setattr(paper, key, obj)


def _restore_files(
    paper: SessionPaper, version: SessionPaperVersion, *, user: SessionUser | None, notes: list[str]
) -> None:
    note = f"Wiederhergestellt aus Fassung {version.number} der Vorlage"
    current = {session_file.pk: session_file for session_file in paper.files.all()}
    for entry in version.files.select_related("blob").order_by("position"):
        blob = entry.blob
        if blob is None or not blob.is_available:
            notes.append(f"„{entry.name}“: Der Inhalt ist nicht mehr vorhanden und wurde nicht wiederhergestellt.")
            continue
        session_file = current.get(entry.attachment_id)
        if session_file is not None:
            file_version_service.restore_content(
                session_file, blob, user=user, name=entry.name, mime_type=entry.mime_type, note=note
            )
            continue
        file_version_service.recreate_attachment(
            paper,
            entry.attachment_id,
            blob,
            user=user,
            name=entry.name,
            mime_type=entry.mime_type,
            note=note,
        )
        notes.append(f"„{entry.name}“ wurde wieder angelegt – nichtöffentlich, bitte prüfen.")
    in_version = set(version.files.values_list("attachment_id", flat=True))
    kept = [session_file.name for pk, session_file in current.items() if pk not in in_version]
    if kept:
        notes.append("Später hinzugefügte Anlagen bleiben erhalten: " + ", ".join(f"„{name}“" for name in kept))


def restore(paper: SessionPaper, version: SessionPaperVersion, *, user: SessionUser | None) -> RestoreResult:
    """
    Fassung als neue Fassung wiederherstellen – die Historie bleibt unberührt.

    1. Weicht der aktuelle Stand von der letzten Fassung ab, wird er vorher gesichert.
    2. Texte, Angaben und Anlagen-Inhalte kommen aus der Fassung; gelöschte Anlagen werden
       nichtöffentlich wieder angelegt, später hinzugefügte bleiben erhalten.
    3. Der Stand danach wird als neue Fassung „Wiederherstellung“ gesichert.
    """
    blocker = restore_blocker(paper)
    if blocker:
        raise RestoreRefusedError(blocker)
    if version.paper_id != paper.pk:
        raise RestoreRefusedError("Die Fassung gehört zu einer anderen Vorlage.")
    notes: list[str] = []
    with transaction.atomic():
        latest = Version.objects.filter(paper=paper).order_by("-number").first()
        if latest is None or latest.fingerprint != current_fingerprint(paper):
            snapshot(
                paper,
                trigger=Version.TRIGGER_BACKUP,
                user=user,
                note=f"Stand vor der Wiederherstellung von Fassung {version.number}",
            )
        for name in RESTORED_FIELDS:
            setattr(paper, name, getattr(version, name))
        _restore_relations(paper, version.details or {}, notes)
        paper.save()
        _restore_files(paper, version, user=user, notes=notes)
        restored = snapshot(
            paper,
            trigger=Version.TRIGGER_RESTORE,
            user=user,
            note=f"Wiederhergestellt aus Fassung {version.number}",
            restored_from=version,
        )
        _log_event(
            "update",
            paper,
            user=user,
            changes={"fassung_wiederhergestellt": version.number, "neue_fassung": restored.number},
        )
    return RestoreResult(version=restored, notes=notes)


def save_manually(paper: SessionPaper, *, user: SessionUser | None, note: str) -> SessionPaperVersion:
    """„Fassung sichern“: aktuellen Stand von Hand festhalten (mit Audit-Eintrag)."""
    version = snapshot(paper, trigger=Version.TRIGGER_MANUAL, user=user, note=note.strip())
    _log_event("create", version, user=user, changes={"fassung": version.number, "bemerkung": version.note})
    return version
