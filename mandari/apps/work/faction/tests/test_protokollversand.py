# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatischer Protokollversand (Issue #871): Organisationseinstellung mit Zeitpunkt, standardmäßig aus, je Sitzung
genau einmal, ältere Protokolle gehen beim Einschalten nicht nachträglich raus, Fassung nach Leserecht.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core import mail

from apps.work.faction.models import FactionAuditLog, FactionMeeting
from apps.work.faction.protocol_dispatch import run_faction_protocol_pass
from apps.work.organization import services as org_services

EINGESCHALTET = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
SITZUNG = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
ENDE = SITZUNG + timedelta(hours=2)
LESEN = ["faction.view_public", "protocols.view_public"]
VOLL = ["faction.view_public", "faction.view_non_public", "protocols.view_public", "protocols.view_full"]


def _einschalten(org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch, modus: str, stunden: int) -> Any:
    """Über den Einstellungsdienst einschalten (setzt den Zeitpunkt des Einschaltens)."""
    vorsitz = make_member(org, ["faction.manage"], email="vorsitz@example.org")
    with monkeypatch.context() as m:
        m.setattr("django.utils.timezone.now", lambda: EINGESCHALTET)
        org_services.save_faction_settings(
            org, vorsitz, {"protocol_dispatch": modus, "protocol_dispatch_delay_hours": str(stunden)}
        )
    org.refresh_from_db()
    return vorsitz


def _sitzung(org: Any, start: datetime = SITZUNG, **felder: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=start, end=start + timedelta(hours=2), **felder
    )


@pytest.mark.django_db
def test_protokollversand_standardmaessig_aus(org: Any, make_member: Any) -> None:
    make_member(org, LESEN, email="mitglied@example.org")
    meeting = _sitzung(org, status="completed")

    mail.outbox = []
    run_faction_protocol_pass(now=ENDE + timedelta(days=3))

    meeting.refresh_from_db()
    assert mail.outbox == []
    assert meeting.protocol_sent_at is None


@pytest.mark.django_db
def test_nach_sitzungsende_genau_einmal_in_der_passenden_fassung(
    org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _einschalten(org, make_member, monkeypatch, "after_completion", 24)
    lesen = make_member(org, LESEN, email="lesen@example.org")
    vereidigt = make_member(org, VOLL, email="vereidigt@example.org")
    vereidigt.is_sworn_in = True
    vereidigt.save(update_fields=["is_sworn_in"])
    make_member(org, ["faction.view_public"], email="ohne-protokollrecht@example.org")
    meeting = _sitzung(org, status="completed")

    mail.outbox = []
    run_faction_protocol_pass(now=ENDE + timedelta(hours=23, minutes=59))
    assert mail.outbox == []

    stats = run_faction_protocol_pass(now=ENDE + timedelta(hours=24))
    assert stats == {"meetings": 1, "sent": 2}
    anhaenge = {m.to[0]: m.attachments[0][0] for m in mail.outbox}
    assert anhaenge == {
        lesen.user.email: "niederschrift-2026-10-05-oeffentlich.pdf",
        vereidigt.user.email: "niederschrift-2026-10-05-intern.pdf",
    }

    run_faction_protocol_pass(now=ENDE + timedelta(hours=25))
    run_faction_protocol_pass(now=ENDE + timedelta(days=3))
    assert len(mail.outbox) == 2
    meeting.refresh_from_db()
    assert meeting.protocol_sent_at == ENDE + timedelta(hours=24)
    assert FactionAuditLog.objects.filter(object_id=meeting.id, action="protocol_sent").count() == 1


@pytest.mark.django_db
def test_offene_sitzung_und_aeltere_protokolle_gehen_nicht_raus(
    org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _einschalten(org, make_member, monkeypatch, "after_completion", 0)
    make_member(org, LESEN, email="mitglied@example.org")
    alt = _sitzung(org, start=EINGESCHALTET - timedelta(days=30), status="completed")
    laeuft = _sitzung(org, status="ongoing")

    mail.outbox = []
    run_faction_protocol_pass(now=ENDE + timedelta(days=1))

    assert mail.outbox == []
    for meeting in (alt, laeuft):
        meeting.refresh_from_db()
        assert meeting.protocol_sent_at is None


@pytest.mark.django_db
def test_nach_genehmigung_mit_verzoegerung(org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _einschalten(org, make_member, monkeypatch, "after_approval", 2)
    make_member(org, LESEN, email="mitglied@example.org")
    meeting = _sitzung(org, status="completed")

    mail.outbox = []
    run_faction_protocol_pass(now=ENDE + timedelta(days=2))
    assert mail.outbox == [], "noch nicht genehmigt"

    genehmigt = ENDE + timedelta(days=7)
    FactionMeeting.objects.filter(pk=meeting.pk).update(
        protocol_approved=True, protocol_approved_at=genehmigt, protocol_status="approved"
    )
    run_faction_protocol_pass(now=genehmigt + timedelta(hours=1))
    assert mail.outbox == []
    run_faction_protocol_pass(now=genehmigt + timedelta(hours=2))
    assert len(mail.outbox) == 1
    assert "genehmigt am" in mail.outbox[0].body


@pytest.mark.django_db
def test_wieder_einschalten_versendet_nichts_nachtraeglich(
    org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    vorsitz = _einschalten(org, make_member, monkeypatch, "after_completion", 0)
    make_member(org, LESEN, email="mitglied@example.org")
    org_services.save_faction_settings(org, vorsitz, {"protocol_dispatch": "off"})
    meeting = _sitzung(org, status="completed")
    spaeter = ENDE + timedelta(days=5)
    with monkeypatch.context() as m:
        m.setattr("django.utils.timezone.now", lambda: spaeter)
        org_services.save_faction_settings(org, vorsitz, {"protocol_dispatch": "after_completion"})

    mail.outbox = []
    run_faction_protocol_pass(now=spaeter + timedelta(hours=1))

    meeting.refresh_from_db()
    assert mail.outbox == []
    assert meeting.protocol_sent_at is None


def test_zeitplan_fuer_den_protokollversand(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.events.schedule import Catchup, Every, autodiscover, registry

    autodiscover()
    eintrag = registry.get("apps.work.schedules.fraktionsprotokolle_versenden")
    assert eintrag is not None
    assert eintrag.trigger == Every(timedelta(minutes=15))
    assert eintrag.catchup == Catchup.NACHHOLEN
    assert eintrag.task.queue_name == "default"

    aufrufe: list[int] = []
    monkeypatch.setattr("apps.work.schedules.run_faction_protocol_pass", lambda *a, **k: aufrufe.append(1))
    eintrag.task.call()
    assert aufrufe == [1]
