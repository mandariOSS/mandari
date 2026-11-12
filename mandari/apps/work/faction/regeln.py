"""
Regeln der Fraktionssitzung, die mehrere Wege teilen.

Diese Linie kennt nur die Sperre nach genehmigtem Protokoll (Issue #63): Beschlüsse und Tagesordnung einer Sitzung
sind danach fest, gleich über welchen Weg die Änderung kommt.
"""

from __future__ import annotations

from typing import Any

PROTOKOLL_GENEHMIGT = "Das Protokoll dieser Sitzung ist genehmigt. Korrekturen sind nur als Nachtrag möglich."


class ProtokollGenehmigtError(ValueError):
    """Änderung an Beschluss oder Tagesordnung einer Sitzung mit genehmigtem Protokoll (Issue #63)."""

    def __init__(self) -> None:
        super().__init__(PROTOKOLL_GENEHMIGT)


def pruefe_protokoll_offen(meeting: Any) -> None:
    """
    ``ProtokollGenehmigtError``, wenn das Protokoll der Sitzung genehmigt ist.

    Sperrt die Sitzung bis zum Ende der laufenden Transaktion (``agenda.lock_meeting``): Die Genehmigung schreibt
    dieselbe Zeile und wartet, bis die Änderung fertig ist, oder sie war vorher fertig und wird hier gesehen. Nur
    innerhalb von ``transaction.atomic``.
    """
    from .agenda import lock_meeting
    from .models import FactionMeeting

    lock_meeting(meeting)
    if FactionMeeting.objects.filter(pk=meeting.pk, protocol_approved=True).exists():
        raise ProtokollGenehmigtError()
