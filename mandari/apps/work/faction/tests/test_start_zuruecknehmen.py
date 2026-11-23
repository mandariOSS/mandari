# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Start einer Fraktionssitzung zurücknehmen (Issue #1566).

Geprüft wird:

- Gestartet und beendet: Zurücknehmen setzt Status, Beginn, geplantes Ende und Protokollstatus zurück.
- Nur gestartet: Das Ende bleibt, die Sitzung steht wieder auf „eingeladen“.
- Ohne versandte Einladung: zurück auf „geplant“.
- Ende ohne Änderungshistorie: aus der Dauer der Reihe, sonst ohne Ende.
- Rechte und Grenzen: nur mit ``faction.start``, nicht nach bestätigter Anwesenheit oder genehmigtem Protokoll;
  der Menüpunkt erscheint nur, wenn das Zurücknehmen geht.
- Gleichzeitig: Genehmigung und Protokollversand, die die Sitzung noch als beendet geladen haben, greifen nach dem
  Zurücknehmen nicht mehr.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction import protocol_dispatch, zuruecknehmen
from apps.work.faction.models import (
    FactionAttendance,
    FactionAuditLog,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionMeetingType,
    FactionProtocolEntry,
)
from apps.work.faction.services import ProtocolApprovalService

pytestmark = pytest.mark.django_db


@pytest.fixture
def welt(org: Organization, make_member: Any) -> dict[str, Any]:
    Organization.objects.filter(pk=org.pk).update(work_new_design=True)
    org.refresh_from_db()
    vorsitz = make_member(org, [], email="vera@example.org", is_admin=True)
    mitglied = make_member(org, ["faction.view_public"], email="max@example.org")
    start = timezone.now() + timedelta(minutes=20)
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Wöchentliche Fraktionssitzung",
        start=start,
        end=start + timedelta(hours=2),
        status="invited",
        invitation_sent=True,
        invitation_sent_at=timezone.now() - timedelta(days=2),
        created_by=vorsitz,
    )
    for m in (vorsitz, mitglied):
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    return {"org": org, "vorsitz": vorsitz, "mitglied": mitglied, "meeting": meeting}


def _aktion(welt: dict[str, Any], client: Any, action: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})
    return client.post(url, {"action": action})


def _seite(welt: dict[str, Any], client: Any) -> str:
    url = reverse("work:faction_detail", kwargs={"org_slug": welt["org"].slug, "meeting_id": welt["meeting"].pk})
    antwort = client.get(url)
    assert antwort.status_code == 200
    return str(antwort.content.decode())


def test_gestartet_und_beendet_zuruecknehmen(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    geplant = meeting.end
    client = client_for(welt["vorsitz"].user)
    _aktion(welt, client, "start")
    _aktion(welt, client, "end")
    meeting.refresh_from_db()
    assert meeting.status == "completed" and meeting.end != geplant
    FactionMeeting.objects.filter(pk=meeting.pk).update(protocol_status="pending")
    FactionProtocolEntry.objects.create(meeting=meeting, entry_type="note", created_by=welt["vorsitz"], order=1)

    html = _seite(welt, client)
    assert 'value="undo_start"' in html and "Start zurücknehmen …" in html

    _aktion(welt, client, "undo_start")
    meeting.refresh_from_db()
    assert meeting.status == "invited"
    assert meeting.started_at is None
    assert meeting.end == geplant
    assert meeting.protocol_status == "draft"
    # Einträge bleiben, die Änderungshistorie hält das Zurücknehmen fest
    assert FactionProtocolEntry.objects.filter(meeting=meeting).count() == 1
    letzter = FactionAuditLog.objects.filter(object_id=meeting.pk).order_by("-created_at").first()
    assert letzter is not None and letzter.changes["status"] == {"alt": "completed", "neu": "invited"}
    # Danach lässt sich die Sitzung wieder starten
    _aktion(welt, client, "start")
    meeting.refresh_from_db()
    assert meeting.status == "ongoing" and meeting.started_at is not None


def test_laufende_sitzung_zuruecknehmen(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    geplant = meeting.end
    client = client_for(welt["vorsitz"].user)
    _aktion(welt, client, "start")
    assert 'value="undo_start"' in _seite(welt, client)

    _aktion(welt, client, "undo_start")
    meeting.refresh_from_db()
    assert (meeting.status, meeting.started_at, meeting.end) == ("invited", None, geplant)
    assert 'value="undo_start"' not in _seite(welt, client)


def test_ohne_einladung_zurueck_auf_geplant(welt: dict[str, Any]) -> None:
    meeting = welt["meeting"]
    FactionMeeting.objects.filter(pk=meeting.pk).update(
        status="ongoing", started_at=timezone.now(), invitation_sent=False, invitation_sent_at=None
    )
    assert zuruecknehmen.zuruecknehmen(meeting)
    meeting.refresh_from_db()
    assert meeting.status == "planned"


def test_ende_ohne_historie(welt: dict[str, Any]) -> None:
    meeting = welt["meeting"]
    FactionMeeting.objects.filter(pk=meeting.pk).update(status="completed", started_at=timezone.now())
    meeting.refresh_from_db()
    assert zuruecknehmen.geplantes_ende(meeting) is None

    meeting.schedule = FactionMeetingSchedule.objects.create(
        organization=welt["org"],
        name="Wöchentliche Fraktionssitzung",
        recurrence="weekly",
        weekday=0,
        time=time(18, 0),
        duration_minutes=90,
        meeting_type=FactionMeetingType.default_for(welt["org"]),
    )
    assert zuruecknehmen.geplantes_ende(meeting) == meeting.start + timedelta(minutes=90)


def test_ueber_statusauswahl_abgeschlossen_ende_bleibt(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    geplant = meeting.end
    client = client_for(welt["vorsitz"].user)
    _aktion(welt, client, "start")
    url = reverse("work:faction_action", kwargs={"org_slug": welt["org"].slug, "meeting_id": meeting.pk})
    client.post(url, {"action": "update_status", "status": "completed"})
    meeting.refresh_from_db()
    assert meeting.status == "completed" and meeting.end == geplant

    assert zuruecknehmen.zuruecknehmen(meeting)
    meeting.refresh_from_db()
    assert (meeting.status, meeting.end) == ("invited", geplant)


def _beendet_und_zurueckgenommen(welt: dict[str, Any], client_for: Any) -> FactionMeeting:
    """Gestartet und beendet; geliefert wird der Stand davor, wie ihn ein gleichzeitiger Vorgang geladen hat."""
    client = client_for(welt["vorsitz"].user)
    _aktion(welt, client, "start")
    _aktion(welt, client, "end")
    vorher = FactionMeeting.objects.get(pk=welt["meeting"].pk)
    assert vorher.status == "completed"
    assert zuruecknehmen.zuruecknehmen(welt["meeting"])
    return vorher


def test_gleichzeitige_genehmigung_nach_zuruecknehmen(welt: dict[str, Any], client_for: Any) -> None:
    vorher = _beendet_und_zurueckgenommen(welt, client_for)

    dienst = cast(Any, ProtocolApprovalService)
    assert dienst.approve_protocol(vorher, approved_in_meeting=None, approved_by=welt["vorsitz"]) is False
    meeting = FactionMeeting.objects.get(pk=vorher.pk)
    assert (meeting.status, meeting.protocol_approved, meeting.protocol_status) == ("invited", False, "draft")


def test_gleichzeitiger_protokollversand_nach_zuruecknehmen(
    welt: dict[str, Any], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    vorher = _beendet_und_zurueckgenommen(welt, client_for)
    einstellungen = {
        "protocol_dispatch": "after_completion",
        "protocol_dispatch_delay_hours": 0,
        "protocol_dispatch_since": timezone.now() - timedelta(days=1),
    }
    versendet: list[Any] = []

    def senden(meeting: Any) -> int:
        versendet.append(meeting)
        return 1

    monkeypatch.setattr(protocol_dispatch, "_candidates", lambda now: iter([(vorher, einstellungen)]))
    monkeypatch.setattr(protocol_dispatch, "send_protocol", senden)

    statistik = protocol_dispatch.run_faction_protocol_pass(now=timezone.now() + timedelta(hours=3))

    assert versendet == [] and statistik["meetings"] == 0
    assert FactionMeeting.objects.get(pk=vorher.pk).protocol_sent_at is None


def test_ohne_recht_kein_zuruecknehmen(welt: dict[str, Any], client_for: Any) -> None:
    meeting = welt["meeting"]
    _aktion(welt, client_for(welt["vorsitz"].user), "start")
    client = client_for(welt["mitglied"].user)
    assert 'value="undo_start"' not in _seite(welt, client)
    _aktion(welt, client, "undo_start")
    meeting.refresh_from_db()
    assert meeting.status == "ongoing"


@pytest.mark.parametrize("sperre", ["bestaetigt", "genehmigt", "verschickt"])
def test_nach_bestaetigung_oder_genehmigung_nicht_mehr(welt: dict[str, Any], client_for: Any, sperre: str) -> None:
    meeting = welt["meeting"]
    client = client_for(welt["vorsitz"].user)
    _aktion(welt, client, "start")
    _aktion(welt, client, "end")
    sperren: dict[str, dict[str, Any]] = {
        "bestaetigt": {"attendance_confirmed_at": timezone.now()},
        "genehmigt": {"protocol_approved": True, "protocol_status": "approved"},
        "verschickt": {"protocol_sent_at": timezone.now()},
    }
    felder = sperren[sperre]
    FactionMeeting.objects.filter(pk=meeting.pk).update(**felder)
    assert 'value="undo_start"' not in _seite(welt, client)
    _aktion(welt, client, "undo_start")
    meeting.refresh_from_db()
    assert meeting.status == "completed"
