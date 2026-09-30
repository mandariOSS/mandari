# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erinnerungen zu Ladungen je Mandant einstellbar (Issue #619).

Knopf „Jetzt erinnern“ und täglicher Lauf nutzen dieselbe Regel und dieselben Einstellungen:
an/aus, Anlass, Empfängerkreis und – nur für den Lauf – mehrere Zeitpunkte. Standard wie bisher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAttendance,
    SessionAuditLog,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionReminderLog,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import invitation_service, reminder_service, rsvp_reminders

pytestmark = pytest.mark.django_db

RESPONSE = SessionTenant.RSVP_REASON_RESPONSE
ACK = SessionTenant.RSVP_REASON_ACKNOWLEDGEMENT
BOTH = SessionTenant.RSVP_REASON_BOTH
ALL = SessionTenant.RSVP_AUDIENCE_ALL
MEMBERS = SessionTenant.RSVP_AUDIENCE_MEMBERS
VOTING = SessionTenant.RSVP_AUDIENCE_VOTING


@dataclass
class Welt:
    tenant: SessionTenant
    meeting: SessionMeeting
    client: Client
    personen: dict[str, SessionPerson]


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


@pytest.fixture
def welt() -> Welt:
    """
    Sitzung in 14 Tagen mit versandter Ladung:

    - Anna (stimmberechtigt): Erhalt bestätigt, keine Zu-/Absage
    - Bert (beratend, ohne Stimmrecht): nichts
    - Gina (Gast): nichts
    - Sven (Stellvertretung ohne Stimmrecht): zugesagt, Erhalt nicht bestätigt
    """
    tenant = SessionTenant.objects.create(name="Stadt Beispiel", slug="beispiel")
    role = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_manage_attendance=True,
        can_manage_settings=True,
    )
    user = cast(Any, UserFactory)(email="dienst@beispiel.example")
    staff = SessionUser.objects.create(user=user, tenant=tenant)
    staff.roles.add(role)
    client = Client()
    client.force_login(user)

    org = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", invitation_period_days=7)
    personen = {
        name: SessionPerson.objects.create(
            tenant=tenant, given_name=name, family_name="Muster", email=f"{name.lower()}@beispiel.example"
        )
        for name in ("Anna", "Bert", "Gina", "Sven")
    }
    SessionOrganizationMembership.objects.create(organization=org, person=personen["Anna"], role="member")
    SessionOrganizationMembership.objects.create(
        organization=org, person=personen["Bert"], role="member", has_voting_rights=False
    )
    SessionOrganizationMembership.objects.create(
        organization=org, person=personen["Gina"], role="guest", has_voting_rights=False
    )
    SessionOrganizationMembership.objects.create(
        organization=org,
        person=personen["Sven"],
        role="member",
        substitute_for=personen["Anna"],
        has_voting_rights=False,
    )
    start = (timezone.now() + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Bauausschuss", organization=org, start=start, meeting_state="scheduled"
    )
    invitation_service.send_invitations(meeting, sent_by=staff)
    anna = SessionInvitationRecipient.objects.get(dispatch__meeting=meeting, person=personen["Anna"])
    anna.acknowledged_at = timezone.now()
    anna.save(update_fields=["acknowledged_at"])
    SessionAttendance.objects.create(meeting=meeting, person=personen["Sven"], status="confirmed")
    meeting.refresh_from_db()
    mail.outbox = []
    return Welt(tenant, meeting, client, personen)


def _einstellen(welt: Welt, **werte: Any) -> None:
    welt.tenant.reminder_settings = werte
    welt.tenant.save(update_fields=["reminder_settings"])
    welt.meeting.tenant = welt.tenant


def _in_tagen(welt: Welt, tage: int) -> None:
    start = (timezone.now() + timedelta(days=tage)).replace(hour=17, minute=0, second=0, microsecond=0)
    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(start=start)
    welt.meeting.refresh_from_db()


def _knopf(welt: Welt) -> set[str]:
    mail.outbox = []
    welt.client.post(f"/session/beispiel/meetings/{welt.meeting.id}/invitation/erinnern/")
    return {addr.split("@")[0] for m in mail.outbox for addr in m.to}


def _lauf(welt: Welt) -> set[str]:
    mail.outbox = []
    reminder_service.run_for_tenant(SessionTenant.objects.get(pk=welt.tenant.pk))
    return {addr.split("@")[0] for m in mail.outbox if "Erinnerung" in m.subject for addr in m.to}


class TestStandard:
    def test_wie_bisher(self) -> None:
        config = SessionTenant(name="x", slug="x").reminder_config()
        assert config["rsvp_enabled"] is True
        assert (config["rsvp_reason"], config["rsvp_audience"], config["rsvp_days"]) == (RESPONSE, ALL, [5])
        assert config["rsvp_days_before"] == 5

    def test_bisherige_vorlaufzeit_bleibt(self) -> None:
        tenant = SessionTenant(name="x", slug="x", reminder_settings={"rsvp_days_before": 3})
        assert tenant.reminder_config()["rsvp_days"] == [3]

    def test_zeitpunkte_bereinigt(self) -> None:
        assert SessionTenant.parse_rsvp_days("2, 7; 7 99 x -1") == [60, 7, 2]
        assert SessionTenant.parse_rsvp_days([1, 2, 3, 4]) == [4, 3, 2]
        assert SessionTenant.parse_rsvp_days("") == []


class TestDieselbeRegel:
    @pytest.mark.parametrize(
        ("anlass", "kreis", "erwartet"),
        [
            (RESPONSE, ALL, {"anna", "bert", "gina"}),
            (ACK, ALL, {"bert", "gina"}),
            (BOTH, ALL, {"anna", "bert", "gina", "sven"}),
            (RESPONSE, MEMBERS, {"anna", "bert"}),
            (BOTH, MEMBERS, {"anna", "bert", "sven"}),
            (RESPONSE, VOTING, {"anna"}),
        ],
    )
    def test_knopf_und_lauf(self, welt: Welt, anlass: str, kreis: str, erwartet: set[str]) -> None:
        _einstellen(welt, rsvp_reason=anlass, rsvp_audience=kreis, rsvp_days=[5])

        assert {t.person.given_name.lower() for t in rsvp_reminders.targets(welt.meeting)} == erwartet
        assert _knopf(welt) == erwartet
        _in_tagen(welt, 3)
        assert _lauf(welt) == erwartet

    def test_mail_nennt_was_fehlt(self, welt: Welt) -> None:
        _einstellen(welt, rsvp_reason=BOTH)
        _knopf(welt)

        anna = next(m for m in mail.outbox if m.to == ["anna@beispiel.example"])
        sven = next(m for m in mail.outbox if m.to == ["sven@beispiel.example"])
        assert "Rückmeldung" in anna.subject and "Zu- oder Absage" in anna.body
        assert "Empfang bestätigen" in sven.subject and "/ladung/" in sven.body
        assert SessionInvitationRecipient.objects.get(person=welt.personen["Sven"]).reminder_count == 1

    def test_uebersicht_nennt_regel_und_zahl(self, welt: Welt) -> None:
        _einstellen(welt, rsvp_reason=ACK)
        seite = welt.client.get(f"/session/beispiel/meetings/{welt.meeting.id}/invitation/rueckmeldungen/")
        inhalt = seite.content.decode()
        assert "2 Empfänger zu erinnern" in inhalt
        assert "Empfangsbestätigung fehlt" in inhalt


class TestAbschalten:
    def test_weder_knopf_noch_lauf_ladung_unberuehrt(self, welt: Welt) -> None:
        _einstellen(welt, rsvp_enabled=False)
        _in_tagen(welt, 3)

        seite = welt.client.get(f"/session/beispiel/meetings/{welt.meeting.id}/invitation/rueckmeldungen/")
        assert 'data-testid="erinnerung-aus"' in seite.content.decode()
        assert _knopf(welt) == set()
        assert _lauf(welt) == set()
        assert not SessionAuditLog.objects.filter(changes__has_key="erinnerung_ladung").exists()
        # Die Ladung selbst bleibt: Nachladung geht weiter raus
        mail.outbox = []
        invitation_service.send_invitations(welt.meeting, sent_by=None, dispatch_type="supplementary")
        assert mail.outbox


class TestZeitpunkte:
    def test_sieben_und_zwei_tage(self, welt: Welt) -> None:
        _einstellen(welt, rsvp_days=[7, 2])

        _in_tagen(welt, 9)
        assert _lauf(welt) == set(), "noch kein Zeitpunkt erreicht"
        _in_tagen(welt, 6)
        assert _lauf(welt) == {"anna", "bert", "gina"}
        assert _lauf(welt) == set(), "je Zeitpunkt nur einmal"
        _in_tagen(welt, 2)
        assert _lauf(welt) == {"anna", "bert", "gina"}
        _in_tagen(welt, 1)
        assert _lauf(welt) == set()

    def test_spaete_ladung_nur_die_faellige_erinnerung(self, welt: Welt) -> None:
        _einstellen(welt, rsvp_days=[7, 2])
        _in_tagen(welt, 1)

        assert _lauf(welt) == {"anna", "bert", "gina"}
        _in_tagen(welt, 0)
        assert _lauf(welt) == set()

    def test_erinnerung_von_frueher_gilt_fuer_den_ersten_zeitpunkt(self, welt: Welt) -> None:
        anna = welt.personen["Anna"]
        SessionReminderLog.objects.create(
            tenant=welt.tenant, kind="attendance_rsvp", dedup_key=f"{welt.meeting.id}:{anna.id}"
        )
        _einstellen(welt, rsvp_days=[7, 2])

        _in_tagen(welt, 6)
        assert "anna" not in _lauf(welt)
        _in_tagen(welt, 2)
        assert "anna" in _lauf(welt)


class TestEinstellungen:
    def test_speichern_und_anzeigen(self, welt: Welt) -> None:
        antwort = welt.client.post(
            "/session/beispiel/settings/reminders/",
            {"rsvp_enabled": "on", "rsvp_reason": BOTH, "rsvp_audience": VOTING, "rsvp_days": "2, 7"},
        )

        assert antwort.status_code == 302
        config = SessionTenant.objects.get(pk=welt.tenant.pk).reminder_config()
        assert (config["rsvp_reason"], config["rsvp_audience"], config["rsvp_days"]) == (BOTH, VOTING, [7, 2])
        assert config["rsvp_days_before"] == 7
        assert SessionAuditLog.objects.filter(tenant=welt.tenant, changes__has_key="erinnerungen").exists()
        inhalt = welt.client.get("/session/beispiel/settings/").content.decode()
        assert 'value="7, 2"' in inhalt
        assert f'value="{VOTING}" selected' in inhalt

    def test_unbekanntes_faellt_auf_standard(self, welt: Welt) -> None:
        welt.client.post(
            "/session/beispiel/settings/reminders/",
            {"rsvp_enabled": "on", "rsvp_reason": "unsinn", "rsvp_audience": "", "rsvp_days": "x"},
        )

        config = SessionTenant.objects.get(pk=welt.tenant.pk).reminder_config()
        assert (config["rsvp_reason"], config["rsvp_audience"], config["rsvp_days"]) == (RESPONSE, ALL, [5])

    def test_abschalten_ueber_formular(self, welt: Welt) -> None:
        welt.client.post("/session/beispiel/settings/reminders/", {"rsvp_days": "5"})
        assert SessionTenant.objects.get(pk=welt.tenant.pk).reminder_config()["rsvp_enabled"] is False
