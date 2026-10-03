# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnungsvorlagen (Issue #757), z. B. „Konstituierende Sitzung (Niedersachsen)“.

Eine Vorlage hängt ihre Punkte an die Tagesordnung einer Sitzung an – vor die Ende-TOPs, mit Kennzeichen „Wahl“,
wo gewählt wird. Vorlagen mit Länderkürzel gibt es nur für Mandanten mit diesem Landesprofil. Verlangt die
Vorlage Präsenz (konstituierende Sitzung: jedes Mitglied kann eine geheime Wahl verlangen, mit Zugeschalteten
wäre sie unzulässig), stellt das Übernehmen eine noch nicht geladene Sitzung auf Präsenz um; nach der Ladung
bleibt es bei einem Hinweis. Der Assistent für den Wahlperiodenwechsel (#155) nutzt dieselbe Vorlage.

Die Vorlagen stehen in ``apps/session/presets/tagesordnungen.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db import transaction

from apps.session.models import SessionAgendaItem, SessionMeeting

TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "presets" / "tagesordnungen.json"


@dataclass(frozen=True)
class AgendaTemplate:
    key: str
    label: str
    state_profile: str
    presence: bool
    hint: str
    #: (Betreff, Wahl)
    items: tuple[tuple[str, bool], ...]


@dataclass
class ApplyResult:
    created: int
    format_changed: bool
    #: Hinweis, wenn die Sitzung nicht in Präsenz stattfindet und nicht mehr umgestellt wurde
    warning: str = ""


def load(path: Path | None = None) -> dict[str, AgendaTemplate]:
    """Vorlagen lesen und prüfen; ``ValueError`` bei ungültiger Datei (Test der mitgelieferten Datei)."""
    data = json.loads((path or TEMPLATE_FILE).read_text(encoding="utf-8"))
    templates: dict[str, AgendaTemplate] = {}
    for key, raw in (data.get("vorlagen") or {}).items():
        items = []
        for top in raw.get("tops") or []:
            name = str(top.get("name") or "").strip()
            if not name or len(name) > 500:
                raise ValueError(f"Tagesordnungsvorlage {key}: Punkt ohne Betreff oder zu lang")
            items.append((name, bool(top.get("wahl", False))))
        if not items:
            raise ValueError(f"Tagesordnungsvorlage {key}: keine Punkte")
        templates[str(key)] = AgendaTemplate(
            key=str(key),
            label=str(raw.get("bezeichnung") or key),
            state_profile=str(raw.get("landesprofil") or ""),
            presence=bool(raw.get("praesenz", False)),
            hint=str(raw.get("hinweis") or ""),
            items=tuple(items),
        )
    return templates


def available(tenant: Any) -> list[AgendaTemplate]:
    """Vorlagen für den Mandanten: ohne Länderkürzel oder mit dem Kürzel seines Landesprofils."""
    code = tenant.state_profile_id or ""
    return [template for template in load().values() if not template.state_profile or template.state_profile == code]


def apply(meeting: SessionMeeting, template: AgendaTemplate) -> ApplyResult:
    """
    Punkte der Vorlage an die Tagesordnung anhängen (öffentlicher Teil einer öffentlichen Sitzung, sonst
    nichtöffentlich) und neu nummerieren.

    Raises:
        protocol_lock.ProtocolLockedError: Niederschrift genehmigt – keine neuen TOPs
    """
    from apps.session.services import agenda_service

    supplementary = bool(meeting.invitation_sent_at) or meeting.meeting_state == "invitation_sent"
    with transaction.atomic():
        for name, election in template.items:
            order = agenda_service.insertion_order(meeting, is_public=meeting.is_public)
            SessionAgendaItem.objects.create(
                meeting=meeting,
                number="?",
                order=order,
                name=name,
                is_public=meeting.is_public,
                is_election=election,
                is_supplementary=supplementary,
            )
        agenda_service.renumber_agenda(meeting)
        result = ApplyResult(created=len(template.items), format_changed=False)
        if template.presence and meeting.format != SessionMeeting.FORMAT_PRESENCE:
            if not supplementary and meeting.meeting_state in ("draft", "scheduled"):
                meeting.format = SessionMeeting.FORMAT_PRESENCE
                meeting.save(update_fields=["format", "updated_at"])
                result.format_changed = True
            else:
                result.warning = (
                    "Die Sitzung ist bereits geladen und nicht als Präsenzsitzung angesetzt. " + template.hint
                ).strip()
    return result
