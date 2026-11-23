# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Start einer Fraktionssitzung zurücknehmen (Issue #1566): versehentlich gestartet oder gleich wieder beendet.

Wer eine Sitzung starten darf (``faction.start``), kann den Start zurücknehmen, solange die Sitzung läuft oder beendet
ist und weder die Anwesenheit bestätigt noch das Protokoll genehmigt oder verschickt ist. Danach steht die Sitzung
wieder vor dem Beginn:

- Status „eingeladen“, ohne versandte Einladung „geplant“ (gestartet wird nur aus diesen beiden).
- Kein tatsächlicher Beginn mehr (``started_at``).
- Das geplante Ende, das „Sitzung beenden“ mit der Uhrzeit des Beendens überschrieben hat: aus der
  Änderungshistorie, sonst Beginn plus Dauer der Sitzungsreihe, sonst ohne Ende.
- Das Protokoll wieder im Entwurf, wenn es nach dem Beenden „zur Genehmigung“ stand.

Protokolleinträge, Notizen, Abstimmungen und Anwesenheiten bleiben erhalten; ein erneuter Start setzt dort fort. Die
Änderungshistorie hält das Zurücknehmen wie jede Statusänderung fest, das Speichern ordnet die Protokollkette neu.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionMeeting

#: Begonnen oder beendet: nur dann gibt es einen Start, den man zurücknehmen kann
BEGONNEN = ("ongoing", "completed")


def zuruecknehmbar(meeting: FactionMeeting) -> bool:
    """Läuft oder beendet, ohne bestätigte Anwesenheit, genehmigtes oder verschicktes Protokoll."""
    return (
        meeting.status in BEGONNEN
        and not meeting.protocol_approved
        and meeting.attendance_confirmed_at is None
        and meeting.protocol_sent_at is None
    )


def darf_zuruecknehmen(membership: Membership, meeting: FactionMeeting) -> bool:
    """Wie Starten und Beenden: ``faction.start``."""
    return membership.has_permission("faction.start") and zuruecknehmbar(meeting)


def geplantes_ende(meeting: FactionMeeting) -> datetime | None:
    """
    Ende vor „Sitzung beenden“. Das Beenden schreibt die Uhrzeit in ``end``; den Wert davor nennt die
    Änderungshistorie (letzter Wechsel auf „beendet“). Hat dieser Wechsel das Ende nicht berührt (abgeschlossen über
    die Statusauswahl), bleibt das Ende, wie es ist. Ohne Eintrag: Beginn plus Dauer der Sitzungsreihe.
    """
    from .models import FactionAuditLog

    eintraege = (
        FactionAuditLog.objects.filter(
            organization_id=meeting.organization_id,
            model_name="FactionMeeting",
            object_id=meeting.pk,
            changes__has_key="status",
        )
        .order_by("-created_at")
        .values_list("changes", flat=True)[:20]
    )
    for changes in eintraege:
        if (changes.get("status") or {}).get("neu") != "completed":
            continue
        if "end" not in changes:
            return meeting.end
        alt = (changes.get("end") or {}).get("alt")
        if not alt:
            return None
        wert = parse_datetime(str(alt))
        if wert is not None:
            return wert if timezone.is_aware(wert) else timezone.make_aware(wert)
        break
    schedule: Any = meeting.schedule
    if schedule is not None and schedule.duration_minutes:
        return meeting.start + timedelta(minutes=schedule.duration_minutes)
    return None


@transaction.atomic
def zuruecknehmen(meeting: FactionMeeting) -> bool:
    """
    Start zurücknehmen. Liefert False, wenn es nicht (mehr) geht. Die Zeile wird gesperrt und frisch gelesen: Eine
    Genehmigung oder Bestätigung, die inzwischen erteilt wurde, ist endgültig (Issue #63).
    """
    from .models import FactionMeeting

    sitzung = FactionMeeting.objects.select_for_update().get(pk=meeting.pk)
    if not zuruecknehmbar(sitzung):
        return False
    felder = ["status", "started_at"]
    if sitzung.status == "completed":
        sitzung.end = geplantes_ende(sitzung)
        felder.append("end")
    if sitzung.protocol_status == "pending":
        sitzung.protocol_status = "draft"
        felder.append("protocol_status")
    sitzung.status = "invited" if sitzung.invitation_sent_at else "planned"
    sitzung.started_at = None
    sitzung.save(update_fields=[*felder, "updated_at"])
    return True
