# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine interne Sitzung von Hand anlegen: aus dem Dialog „Sitzung planen“ und aus einer Terminumfrage (Issue #1489).

Beide Wege legen die Sitzung gleich an: Nummer in ihrer Sitzungsart (Issue #1231), Anwesenheit „eingeladen“ für die
Teilnehmenden der Art ohne Gastzugänge (Issue #1239), auf Wunsch der Organisation der Tagesordnungspunkt zur
Genehmigung des letzten Protokolls und die Standard-Tagesordnung (Issue #872). Die vorherige Sitzung setzt das
Speichern über die Protokollkette (``kette.py``). Die Einladung selbst verschickt wie bisher die Sitzung.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .agenda import apply_standard_agenda
from .models import FactionAttendance, FactionMeeting, FactionMeetingType
from .sitzungsarten import teilnehmende


def neue_sitzung(
    organization: Any,
    membership: Any,
    *,
    title: str,
    start: datetime,
    end: datetime | None = None,
    location: str = "",
    video_link: str = "",
    is_virtual: bool = False,
    description: str = "",
    status: str = "planned",
    rsvp_enabled: bool = False,
    meeting_type: FactionMeetingType | None = None,
    standard_agenda: bool = True,
) -> FactionMeeting:
    """Sitzung anlegen und wie beim Planen von Hand vorbereiten; ohne ``meeting_type`` in der Standardart."""
    from .services import ProtocolApprovalService

    meeting_type = meeting_type or FactionMeetingType.default_for(organization)
    meeting = FactionMeeting(
        organization=organization,
        meeting_type=meeting_type,
        created_by=membership,
        title=title,
        start=start,
        end=end,
        location=location,
        is_virtual=is_virtual or bool(video_link),
        video_link=video_link,
        description=description,
        status=status,
        # Zu- und Absagen sind standardmäßig aus und je Sitzung einschaltbar (Issue #871)
        rsvp_enabled=rsvp_enabled,
        meeting_number=FactionMeeting.get_next_meeting_number(organization, meeting_type),
    )
    meeting.save()

    for member in teilnehmende(meeting_type):
        FactionAttendance.objects.create(meeting=meeting, membership=member, status="invited")

    # Über den Service, damit das Vorprotokoll gleichzeitig in den Status „Zur Genehmigung“ wechselt
    if meeting.get_faction_settings().get("auto_create_approval_item", True):
        ProtocolApprovalService.auto_create_approval_item(meeting)

    if standard_agenda:
        apply_standard_agenda(meeting)
    return meeting
