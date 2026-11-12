# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wer die Tagesordnung einer Fraktionssitzung verwaltet.

Eine Regel für alle Wege (Sitzungsansicht, TOP-Panel, Aktionen), die sie bisher jeweils selbst ausgeschrieben hatten.
"""

from __future__ import annotations

from typing import Any

#: Status, in denen die Sitzungsleitung Sitzung und Tagesordnung bearbeitet
MANAGE_OPEN_STATUSES = ("draft", "planned", "invited", "ongoing")


def manages(meeting: Any, membership: Any) -> bool:
    """
    Verwaltet die Person die Tagesordnung dieser Sitzung (Ersteller:in oder ``faction.manage``)?

    Nie nach genehmigtem Protokoll (Issue #63): Tagesordnung, Beschlüsse und Anlagen sind dann fest, auch wenn
    jemand den Status der Sitzung zurücksetzt. Korrekturen laufen als Nachtrag zum Protokoll.
    """
    from .models import FactionMeeting

    # Stand der Datenbank, nicht der des geladenen Objekts: Die Genehmigung kann nach dem Laden gekommen sein
    if meeting.protocol_approved or FactionMeeting.objects.filter(pk=meeting.pk, protocol_approved=True).exists():
        return False
    return bool(meeting.created_by == membership or membership.has_permission("faction.manage"))


def editable_by(meeting: Any, membership: Any) -> bool:
    """Darf die Person Sitzung und Tagesordnung jetzt bearbeiten (verwaltet sie und die Sitzung ist offen)?"""
    return manages(meeting, membership) and meeting.status in MANAGE_OPEN_STATUSES
