# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen der Reihen-Automatik (Issue #871): Zu- und Absagen und automatische Einladung je Reihe,
TOP-Erinnerung und Protokollversand je Organisation. Mit Berechtigungs- und Mandantentrennungstest.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction.models import FactionMeeting, FactionMeetingSchedule

MANAGE = ["faction.manage", "faction.view_public"]


def settings_url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


@pytest.fixture
def manager(org: Any, make_member: Any) -> Any:
    return make_member(org, MANAGE, email="vorsitz@example.org")


@pytest.fixture
def reihe(org: Any) -> FactionMeetingSchedule:
    return FactionMeetingSchedule.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))


def _automatik(reihe: FactionMeetingSchedule, **felder: str) -> dict[str, str]:
    return {"section": "update_schedule_automation", "schedule_id": str(reihe.id), **felder}


@pytest.mark.django_db
def test_neue_reihe_mit_automatik(org: Any, manager: Any, client_for: Any) -> None:
    client_for(manager.user).post(
        settings_url(org),
        {
            "section": "add_schedule",
            "name": "Wöchentliche Fraktionssitzung",
            "weekday": "0",
            "time": "18:00",
            "rsvp_enabled": "on",
            "auto_invite": "on",
            "auto_invite_weekday": "4",
            "auto_invite_time": "18:00",
        },
    )

    reihe = FactionMeetingSchedule.objects.get(organization=org)
    assert (reihe.rsvp_enabled, reihe.auto_invite, reihe.auto_invite_weekday, reihe.auto_invite_time) == (
        True,
        True,
        4,
        time(18, 0),
    )


@pytest.mark.django_db
def test_neue_reihe_ohne_angaben_hat_alles_aus(org: Any, manager: Any, client_for: Any) -> None:
    client_for(manager.user).post(
        settings_url(org), {"section": "add_schedule", "name": "Reihe", "weekday": "0", "time": "18:00"}
    )

    reihe = FactionMeetingSchedule.objects.get(organization=org)
    assert (reihe.rsvp_enabled, reihe.auto_invite) == (False, False)


@pytest.mark.django_db
def test_automatik_speichern_passt_kommende_termine_an(
    org: Any, manager: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    kuenftig = timezone.now() + timedelta(days=10)
    offen = FactionMeeting.objects.create(organization=org, title="Offen", start=kuenftig, schedule=reihe)
    eingeladen = FactionMeeting.objects.create(
        organization=org, title="Eingeladen", start=kuenftig, schedule=reihe, status="invited", invitation_sent=True
    )

    response = client_for(manager.user).post(
        settings_url(org),
        _automatik(reihe, rsvp_enabled="on", auto_invite="on", auto_invite_weekday="4", auto_invite_time="18:00"),
    )

    reihe.refresh_from_db()
    offen.refresh_from_db()
    eingeladen.refresh_from_db()
    assert reihe.rsvp_enabled and reihe.auto_invite_ready
    assert offen.rsvp_enabled is True
    assert eingeladen.rsvp_enabled is False, "bereits verschickte Einladungen bleiben, wie sie sind"
    assert any("1 kommenden Terminen" in str(m) for m in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_reihe_ausschalten_laesst_vorhandene_zu_und_absagen_unveraendert(
    org: Any, manager: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    """Entscheidung vom 05.10.2026: Umgeschaltet wird nur die Einstellung, Rückmeldungen bleiben vollständig."""
    from apps.work.faction.models import FactionAttendance

    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(rsvp_enabled=True)
    termin = FactionMeeting.objects.create(
        organization=org, title="Termin", start=timezone.now() + timedelta(days=10), schedule=reihe, rsvp_enabled=True
    )
    FactionAttendance.objects.create(
        meeting=termin,
        membership=manager,
        status="declined",
        response_message="Im Urlaub",
        responded_at=timezone.now(),
    )

    def teilnahmen() -> list[Any]:
        return list(FactionAttendance.objects.filter(meeting=termin).values())

    vorher = teilnahmen()
    client = client_for(manager.user)

    client.post(settings_url(org), _automatik(reihe))
    termin.refresh_from_db()
    assert termin.rsvp_enabled is False
    assert teilnahmen() == vorher

    client.post(settings_url(org), _automatik(reihe, rsvp_enabled="on"))
    termin.refresh_from_db()
    assert termin.rsvp_enabled is True
    assert teilnahmen() == vorher


@pytest.mark.django_db
def test_automatische_einladung_braucht_wochentag_und_uhrzeit(
    org: Any, manager: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    response = client_for(manager.user).post(settings_url(org), _automatik(reihe, auto_invite="on"))

    reihe.refresh_from_db()
    assert reihe.auto_invite is False
    assert any("Wochentag und Uhrzeit" in str(m) for m in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_automatik_nur_mit_recht_zur_verwaltung(
    org: Any, make_member: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    mitglied = make_member(org, ["faction.view_public"], email="mitglied@example.org")

    response = client_for(mitglied.user).post(settings_url(org), _automatik(reihe, rsvp_enabled="on"))

    reihe.refresh_from_db()
    assert response.status_code == 403
    assert reihe.rsvp_enabled is False


@pytest.mark.django_db
def test_automatik_fremder_reihe_bleibt_unveraendert(org: Any, manager: Any, client_for: Any) -> None:
    fremd = Organization.objects.create(name="Andere Fraktion", slug="andere-fraktion")
    fremde_reihe = FactionMeetingSchedule.objects.create(organization=fremd, name="Fremd", weekday=0, time=time(18))

    response = client_for(manager.user).post(settings_url(org), _automatik(fremde_reihe, rsvp_enabled="on"))

    fremde_reihe.refresh_from_db()
    assert fremde_reihe.rsvp_enabled is False
    assert any("nicht gefunden" in str(m) for m in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_organisationseinstellungen_fuer_erinnerung_und_protokollversand(
    org: Any, manager: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    client = client_for(manager.user)
    html = client.get(settings_url(org)).content.decode()
    for name in ("agenda_reminder_enabled", "agenda_reminder_hours", "protocol_dispatch", "auto_invite_weekday"):
        assert f'name="{name}"' in html, name
    assert 'value="update_schedule_automation"' in html

    client.post(
        settings_url(org),
        {
            "invitation_mode": "opt_in",
            "invitation_dispatch": "automatic",
            "invitation_lead_hours": "72",
            "agenda_reminder_enabled": "on",
            "agenda_reminder_hours": "48",
            "protocol_dispatch": "after_approval",
            "protocol_dispatch_delay_hours": "12",
        },
    )

    org.refresh_from_db()
    faction = org.settings["faction"]
    assert faction["agenda_reminder_enabled"] is True
    assert faction["agenda_reminder_hours"] == 48
    assert faction["protocol_dispatch"] == "after_approval"
    assert faction["protocol_dispatch_delay_hours"] == 12
    assert faction["protocol_dispatch_since"]


@pytest.mark.django_db
@pytest.mark.parametrize("aktion", ["automatik", "pausieren"])
def test_einstellungen_schreiben_den_erzeugt_bis_stand_nicht_zurueck(
    org: Any, reihe: FactionMeetingSchedule, monkeypatch: pytest.MonkeyPatch, aktion: str
) -> None:
    from datetime import date

    from apps.work.organization import services

    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(generated_until=date(2026, 11, 30))
    veraltet = FactionMeetingSchedule.objects.get(pk=reihe.pk)
    # Der Erzeugungslauf rückt den Stand vor, während das Formular noch mit dem alten Objekt arbeitet
    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(generated_until=date(2026, 12, 28))
    monkeypatch.setattr(services, "_schedule", lambda organization, schedule_id: veraltet)

    if aktion == "automatik":
        services.update_schedule_automation(org, reihe.id, services.ScheduleAutomationInput(rsvp_enabled=True))
    else:
        services.toggle_schedule(org, reihe.id)

    reihe.refresh_from_db()
    assert reihe.generated_until == date(2026, 12, 28)
    if aktion == "automatik":
        assert reihe.rsvp_enabled is True
    else:
        assert reihe.is_active is False


@pytest.mark.django_db
def test_automatik_formular_je_reihe_mit_eigenen_feldern_und_stand(
    org: Any, manager: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    import re

    zweite = FactionMeetingSchedule.objects.create(
        organization=org,
        name="Zweite Reihe",
        weekday=2,
        time=time(18, 0),
        rsvp_enabled=True,
        auto_invite=True,
        auto_invite_weekday=4,
        auto_invite_time=time(17, 30),
    )

    html = client_for(manager.user).get(settings_url(org)).content.decode()

    for key in (reihe.id, zweite.id, "neu"):
        for feld in ("rsvp_enabled", "auto_invite", "auto_invite_weekday", "auto_invite_time"):
            assert html.count(f'id="{feld}_{key}"') == 1, (feld, key)
            assert f'for="{feld}_{key}"' in html, (feld, key)
    assert re.search(rf'name="rsvp_enabled" id="rsvp_enabled_{zweite.id}"\s+checked', html)
    assert re.search(rf'name="auto_invite" id="auto_invite_{zweite.id}"\s+checked', html)
    assert not re.search(rf'name="rsvp_enabled" id="rsvp_enabled_{reihe.id}"\s+checked', html)
    assert not re.search(r'name="rsvp_enabled" id="rsvp_enabled_neu"\s+checked', html)
    assert '<option value="4" selected>Freitag</option>' in html
    assert re.search(rf'name="auto_invite_time" id="auto_invite_time_{zweite.id}"\s+value="17:30"', html)
