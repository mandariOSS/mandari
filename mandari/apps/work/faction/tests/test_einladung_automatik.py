# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatik der Einladung (Issue #871): automatische Einladung zum festen Zeitpunkt einer Reihe (ohne Freigabe,
mit der Tagesordnung von diesem Zeitpunkt, genau einmal), Erinnerung zum Eintragen von TOPs und Zu- und Absagen,
die standardmäßig aus und je Sitzung einschaltbar sind.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from django.core import mail
from django.urls import reverse

from apps.work.faction.invitations import (
    dispatch_invitations,
    dispatch_invitations_once,
    fixed_dispatch_at,
    invitation_dispatch_at,
    run_faction_invitation_pass,
)
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionAuditLog,
    FactionMeeting,
    FactionMeetingSchedule,
)
from apps.work.faction.services import run_faction_reminder_pass

# Montag, 26.10.2026, 18:00 Uhr Ortszeit – einen Tag nach dem Ende der Sommerzeit
MONTAG_18 = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
# Freitag davor, 18:00 Uhr Ortszeit – noch Sommerzeit
FREITAG_18 = datetime(2026, 10, 23, 16, 0, tzinfo=UTC)
FREITAG = 4


def _org_settings(org: Any, **faction: Any) -> None:
    org.settings = {**(org.settings or {}), "faction": {**(org.settings or {}).get("faction", {}), **faction}}
    org.save(update_fields=["settings"])


def _meeting(org: Any, start: datetime = MONTAG_18, **felder: Any) -> FactionMeeting:
    meeting = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung Oktober", start=start, status="planned", **felder
    )
    for membership in org.memberships.filter(is_active=True, is_guest=False):
        FactionAttendance.objects.create(meeting=meeting, membership=membership, status="invited")
    return meeting


def _top(meeting: FactionMeeting, title: str, order: int, **felder: Any) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(
        meeting=meeting, number=str(order), title=title, visibility="public", order=order, **felder
    )


@pytest.fixture
def reihe(org: Any) -> FactionMeetingSchedule:
    return FactionMeetingSchedule.objects.create(
        organization=org,
        name="Wöchentliche Fraktionssitzung",
        weekday=0,
        time=time(18, 0),
        auto_invite=True,
        auto_invite_weekday=FREITAG,
        auto_invite_time=time(18, 0),
    )


# =============================================================================
# Fester Zeitpunkt (Ortszeit, Sommerzeit)
# =============================================================================


def test_fester_zeitpunkt_bleibt_ueber_die_zeitumstellung_18_uhr_ortszeit() -> None:
    # Herbst: Sitzung nach der Umstellung, Einladung davor
    assert fixed_dispatch_at(MONTAG_18, FREITAG, time(18, 0)) == FREITAG_18
    # Frühjahr: Montag, 29.03.2027, 18:00 Sommerzeit -> Freitag, 26.03.2027, 18:00 Winterzeit
    assert fixed_dispatch_at(datetime(2027, 3, 29, 16, 0, tzinfo=UTC), FREITAG, time(18, 0)) == datetime(
        2027, 3, 26, 17, 0, tzinfo=UTC
    )


def test_fester_zeitpunkt_am_sitzungstag_nur_vor_dem_beginn() -> None:
    freitag_19 = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)
    assert fixed_dispatch_at(freitag_19, FREITAG, time(18, 0)) == FREITAG_18
    # Gleiche Uhrzeit wie der Beginn: eine Woche früher
    assert fixed_dispatch_at(FREITAG_18, FREITAG, time(18, 0)) == FREITAG_18 - timedelta(days=7)


# =============================================================================
# Automatische Einladung der Reihe
# =============================================================================


@pytest.mark.django_db
def test_automatische_einladung_zum_festen_zeitpunkt_genau_einmal_mit_aktueller_tagesordnung(
    org: Any, make_member: Any, reihe: FactionMeetingSchedule
) -> None:
    # Freigabe-Modus der Organisation: die Reihe lädt trotzdem ohne Freigabe ein
    _org_settings(org, invitation_dispatch="approval")
    mitglied = make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org, schedule=reihe, scheduled_date=date(2026, 10, 26))
    _top(meeting, "Haushalt 2027", 1)
    assert invitation_dispatch_at(meeting) == FREITAG_18

    mail.outbox = []
    run_faction_invitation_pass(now=FREITAG_18 - timedelta(minutes=1))
    meeting.refresh_from_db()
    assert mail.outbox == []
    assert meeting.invitation_sent is False

    # Nach der Anlage ergänzt: kommt mit; ein offener Vorschlag nicht
    _top(meeting, "Radwegkonzept", 2)
    _top(meeting, "Vorschlag Spielplatz", 3, proposal_status="proposed")

    stats = run_faction_invitation_pass(now=FREITAG_18)
    meeting.refresh_from_db()
    assert stats["dispatched"] == 1
    assert [m.to for m in mail.outbox] == [[mitglied.user.email]]
    text = mail.outbox[0].body
    assert "Haushalt 2027" in text
    assert "Radwegkonzept" in text
    assert "Spielplatz" not in text
    assert meeting.invitation_sent is True
    assert meeting.status == "invited"

    # Wiederholung: kein zweiter Versand
    run_faction_invitation_pass(now=FREITAG_18 + timedelta(minutes=15))
    run_faction_invitation_pass(now=FREITAG_18 + timedelta(hours=2))
    assert len(mail.outbox) == 1
    assert FactionAuditLog.objects.filter(object_id=meeting.id, action="invitation_sent").count() == 1


@pytest.mark.django_db
def test_sitzungsansicht_nennt_den_automatischen_versand(
    org: Any, make_member: Any, client_for: Any, reihe: FactionMeetingSchedule
) -> None:
    _org_settings(org, invitation_dispatch="approval")
    vorsitz = make_member(org, ["faction.view_public", "faction.manage", "faction.invite"], email="v@example.org")
    meeting = _meeting(org, schedule=reihe, scheduled_date=date(2026, 10, 26))
    url = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})

    html = client_for(vorsitz.user).get(url).content.decode()

    assert "Einladung geht automatisch am Fr, 23.10.2026 18:00 Uhr raus" in html
    assert "Einladungsversand freigeben" not in html, "keine Freigabe nötig"


@pytest.mark.django_db
def test_ohne_automatik_der_reihe_bleibt_der_freigabe_modus(org: Any, make_member: Any) -> None:
    _org_settings(org, invitation_dispatch="approval")
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    reihe = FactionMeetingSchedule.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))
    meeting = _meeting(org, schedule=reihe, scheduled_date=date(2026, 10, 26))

    run_faction_invitation_pass(now=MONTAG_18 - timedelta(hours=1))

    meeting.refresh_from_db()
    assert meeting.invitation_sent is False
    assert invitation_dispatch_at(meeting) == MONTAG_18 - timedelta(hours=72)


@pytest.mark.django_db
def test_erstversand_nur_einmal_auch_bei_gleichzeitigem_lauf(org: Any, make_member: Any) -> None:
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org)
    # Ein anderer Lauf hat den Versand gerade beansprucht; dieses Objekt ist noch auf altem Stand
    FactionMeeting.objects.filter(pk=meeting.pk).update(invitation_sent=True)
    mail.outbox = []

    assert dispatch_invitations_once(meeting) is None
    assert mail.outbox == []


@pytest.mark.django_db
def test_gescheiterter_versand_gibt_den_anspruch_zurueck(
    org: Any, make_member: Any, reihe: FactionMeetingSchedule, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org, schedule=reihe, scheduled_date=date(2026, 10, 26))

    def kaputt(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("PDF")

    with monkeypatch.context() as m:
        m.setattr("apps.work.faction.services.FactionMeetingEmailService.send_invitations", kaputt)
        run_faction_invitation_pass(now=FREITAG_18)
    meeting.refresh_from_db()
    assert meeting.invitation_sent is False

    mail.outbox = []
    run_faction_invitation_pass(now=FREITAG_18 + timedelta(minutes=15))
    meeting.refresh_from_db()
    assert meeting.invitation_sent is True
    assert len(mail.outbox) == 1


# =============================================================================
# Erinnerung zum Eintragen von TOPs
# =============================================================================


@pytest.mark.django_db
def test_top_erinnerung_genau_einmal_an_alle_die_tops_eintragen_duerfen(org: Any, make_member: Any) -> None:
    _org_settings(org, agenda_reminder_enabled=True, agenda_reminder_hours=24)
    vorschlag = make_member(org, ["faction.view_public", "agenda.propose"], email="sachkundig@example.org")
    eintrag = make_member(org, ["faction.view_public", "agenda.create"], email="rat@example.org")
    make_member(org, ["faction.view_public"], email="nur-lesen@example.org")
    abgesagt = make_member(org, ["faction.view_public", "agenda.create"], email="abgesagt@example.org")
    meeting = _meeting(org)
    FactionAttendance.objects.filter(meeting=meeting, membership=abgesagt).update(status="declined")
    _top(meeting, "Haushalt 2027", 1)
    # Versand nach Vorlauf der Organisation (72 h): Freitag, 23.10.2026, 19:00 Uhr Ortszeit
    versand = MONTAG_18 - timedelta(hours=72)

    mail.outbox = []
    run_faction_invitation_pass(now=versand - timedelta(hours=24, minutes=1))
    assert mail.outbox == []

    stats = run_faction_invitation_pass(now=versand - timedelta(hours=24))
    assert stats["agenda_reminders"] == 1
    assert sorted(m.to[0] for m in mail.outbox) == sorted([vorschlag.user.email, eintrag.user.email])
    assert "Haushalt 2027" in mail.outbox[0].body
    assert "23.10.2026, 19:00 Uhr" in mail.outbox[0].body
    meeting.refresh_from_db()
    assert meeting.agenda_reminder_sent_at is not None
    assert meeting.invitation_sent is False, "die Erinnerung kommt vor der Einladung"

    run_faction_invitation_pass(now=versand - timedelta(hours=2))
    assert len(mail.outbox) == 2
    assert FactionAuditLog.objects.filter(object_id=meeting.id, action="agenda_reminder_sent").count() == 1


@pytest.mark.django_db
def test_top_erinnerung_standardmaessig_aus(org: Any, make_member: Any) -> None:
    make_member(org, ["faction.view_public", "agenda.create"], email="rat@example.org")
    meeting = _meeting(org)

    mail.outbox = []
    run_faction_invitation_pass(now=MONTAG_18 - timedelta(hours=80))

    meeting.refresh_from_db()
    assert mail.outbox == []
    assert meeting.agenda_reminder_sent_at is None


@pytest.mark.django_db
def test_keine_top_erinnerung_nach_dem_geplanten_versand(org: Any, make_member: Any) -> None:
    _org_settings(org, agenda_reminder_enabled=True, agenda_reminder_hours=24, invitation_dispatch="approval")
    make_member(org, ["faction.view_public", "agenda.create"], email="rat@example.org")
    meeting = _meeting(org)

    run_faction_invitation_pass(now=MONTAG_18 - timedelta(hours=71))

    meeting.refresh_from_db()
    assert meeting.agenda_reminder_sent_at is None


# =============================================================================
# Zu- und Absagen: standardmäßig aus, je Sitzung einschaltbar
# =============================================================================


@pytest.mark.django_db
def test_neue_sitzung_ohne_zu_und_absagen_ausser_eingeschaltet(org: Any, make_member: Any, client_for: Any) -> None:
    chair = make_member(org, ["faction.view_public", "faction.create"], email="vorsitz@example.org")
    client = client_for(chair.user)
    url = reverse("work:faction", kwargs={"org_slug": org.slug})
    felder = {"start_date": "2026-10-26", "start_time": "18:00"}

    client.post(url, {**felder, "title": "Ohne Rückmeldung"})
    client.post(url, {**felder, "title": "Mit Rückmeldung", "rsvp_enabled": "on"})

    assert FactionMeeting.objects.get(title="Ohne Rückmeldung").rsvp_enabled is False
    assert FactionMeeting.objects.get(title="Mit Rückmeldung").rsvp_enabled is True


@pytest.mark.django_db
def test_zu_und_absagen_je_sitzung_einschaltbar(org: Any, make_member: Any, client_for: Any) -> None:
    chair = make_member(org, ["faction.view_public", "faction.manage"], email="vorsitz@example.org")
    meeting = _meeting(org, start=MONTAG_18 + timedelta(days=400))
    client = client_for(chair.user)
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    felder = {"action": "update", "title": meeting.title, "start_date": "2027-11-30", "start_time": "18:00"}

    client.post(url, {**felder, "rsvp_field": "1", "rsvp_enabled": "on"})
    meeting.refresh_from_db()
    assert meeting.rsvp_enabled is True

    # Ohne das Feld (andere Formulare) bleibt der Schalter, mit leerer Checkbox geht er aus
    client.post(url, felder)
    meeting.refresh_from_db()
    assert meeting.rsvp_enabled is True
    client.post(url, {**felder, "rsvp_field": "1"})
    meeting.refresh_from_db()
    assert meeting.rsvp_enabled is False


@pytest.mark.django_db
def test_zusage_nur_wenn_eingeschaltet(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org, start=MONTAG_18 + timedelta(days=400))
    client = client_for(mitglied.user)
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    detail = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})

    assert "Meine Teilnahme" not in client.get(detail).content.decode()
    response = client.post(url, {"action": "respond", "status": "confirmed"})
    assert response.status_code == 403
    assert FactionAttendance.objects.get(meeting=meeting, membership=mitglied).status == "invited"

    FactionMeeting.objects.filter(pk=meeting.pk).update(rsvp_enabled=True)
    assert "Meine Teilnahme" in client.get(detail).content.decode()
    client.post(url, {"action": "respond", "status": "confirmed"})
    assert FactionAttendance.objects.get(meeting=meeting, membership=mitglied).status == "confirmed"


def _teilnahmen(meeting: FactionMeeting) -> list[Any]:
    return sorted(FactionAttendance.objects.filter(meeting=meeting).values(), key=lambda zeile: str(zeile["id"]))


@pytest.mark.django_db
def test_ausschalten_laesst_vorhandene_zu_und_absagen_unveraendert_und_sichtbar(
    org: Any, make_member: Any, client_for: Any
) -> None:
    """Entscheidung vom 05.10.2026: Umgeschaltet wird nur die Einstellung, Rückmeldungen bleiben vollständig."""
    chair = make_member(org, ["faction.view_public", "faction.manage"], email="vorsitz@example.org")
    mitglied = make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org, start=MONTAG_18 + timedelta(days=400), rsvp_enabled=True)
    FactionAttendance.objects.filter(meeting=meeting, membership=chair).update(status="confirmed")
    FactionAttendance.objects.filter(meeting=meeting, membership=mitglied).update(
        status="declined", response_message="Im Urlaub", responded_at=MONTAG_18
    )
    vorher = _teilnahmen(meeting)
    action = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    detail = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    felder = {"action": "update", "title": meeting.title, "start_date": "2027-11-30", "start_time": "18:00"}
    vorsitz = client_for(chair.user)
    leser = client_for(mitglied.user)

    vorsitz.post(action, {**felder, "rsvp_field": "1"})
    meeting.refresh_from_db()
    assert meeting.rsvp_enabled is False
    assert _teilnahmen(meeting) == vorher
    html = leser.get(detail).content.decode()
    assert "Meine Teilnahme" not in html
    assert "Zugesagt" in html and "Abgesagt" in html, "Rückmeldungen bleiben in der Teilnehmerliste sichtbar"

    vorsitz.post(action, {**felder, "rsvp_field": "1", "rsvp_enabled": "on"})
    meeting.refresh_from_db()
    assert meeting.rsvp_enabled is True
    assert _teilnahmen(meeting) == vorher
    assert "Meine Teilnahme" in leser.get(detail).content.decode()


@pytest.mark.django_db
def test_einladung_bittet_nur_mit_eingeschalteten_rueckmeldungen_um_zu_oder_absage(org: Any, make_member: Any) -> None:
    _org_settings(org, invitation_mode="opt_out")
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    ohne = _meeting(org)
    mit = _meeting(org, rsvp_enabled=True)

    mail.outbox = []
    dispatch_invitations(ohne)
    assert "absagen" not in mail.outbox[0].body.lower()
    assert set(ohne.attendances.values_list("status", flat=True)) == {"invited"}, "Opt-out nur mit Rückmeldungen"

    mail.outbox = []
    dispatch_invitations(mit)
    assert "Absagen" in mail.outbox[0].body
    assert set(mit.attendances.values_list("status", flat=True)) == {"confirmed"}


@pytest.mark.django_db
def test_erinnerung_ohne_zusagen_geht_an_alle_eingeladenen(org: Any, make_member: Any) -> None:
    eingeladen = make_member(org, ["faction.view_public"], email="eingeladen@example.org")
    abgesagt = make_member(org, ["faction.view_public"], email="abgesagt@example.org")
    ohne = _meeting(org, invitation_sent=True)
    mit = _meeting(org, invitation_sent=True, rsvp_enabled=True)
    for meeting in (ohne, mit):
        FactionAttendance.objects.filter(meeting=meeting, membership=abgesagt).update(status="declined")
    FactionAttendance.objects.filter(meeting=mit, membership=eingeladen).update(status="invited")

    mail.outbox = []
    run_faction_reminder_pass(now=MONTAG_18 - timedelta(hours=24))

    empfaenger = sorted(m.to[0] for m in mail.outbox)
    # Ohne Rückmeldungen: alle Eingeladenen außer Absagen; mit Rückmeldungen nur Zusagen (hier keine)
    assert empfaenger == [eingeladen.user.email]


# =============================================================================
# Deaktivierte Mitglieder bekommen weder Einladung noch Erinnerung
# =============================================================================


def _deaktivieren(membership: Any) -> None:
    """Wie ``deactivate_member``: Die Teilnahmen an schon angelegten Sitzungen bleiben stehen."""
    membership.is_active = False
    membership.save(update_fields=["is_active"])


@pytest.mark.django_db
@pytest.mark.parametrize("rsvp_enabled", [False, True])
def test_erinnerung_nicht_an_deaktivierte_mitglieder(org: Any, make_member: Any, rsvp_enabled: bool) -> None:
    from apps.work.notifications.models import Notification

    aktiv = make_member(org, ["faction.view_public"], email="aktiv@example.org")
    ausgeschieden = make_member(org, ["faction.view_public"], email="ausgeschieden@example.org")
    meeting = _meeting(
        org, invitation_sent=True, rsvp_enabled=rsvp_enabled, video_link="https://video.example.org/raum"
    )
    if rsvp_enabled:
        FactionAttendance.objects.filter(meeting=meeting).update(status="confirmed")
    _deaktivieren(ausgeschieden)

    mail.outbox = []
    run_faction_reminder_pass(now=MONTAG_18 - timedelta(hours=24))

    assert [m.to for m in mail.outbox] == [[aktiv.user.email]]
    assert not Notification.objects.filter(recipient=ausgeschieden).exists()
    assert Notification.objects.filter(recipient=aktiv).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("update", [False, True])
def test_einladung_nicht_an_deaktivierte_mitglieder(org: Any, make_member: Any, update: bool) -> None:
    from apps.work.notifications.models import Notification

    aktiv = make_member(org, ["faction.view_public"], email="aktiv@example.org")
    ausgeschieden = make_member(org, ["faction.view_public"], email="ausgeschieden@example.org")
    meeting = _meeting(org)
    _top(meeting, "Haushalt 2027", 1)
    _deaktivieren(ausgeschieden)

    mail.outbox = []
    dispatch_invitations(meeting, update=update)

    assert [m.to for m in mail.outbox] == [[aktiv.user.email]]
    assert not Notification.objects.filter(recipient=ausgeschieden).exists()
    assert Notification.objects.filter(recipient=aktiv).count() == 1


# =============================================================================
# Hängender Anspruch auf den Erstversand (Prozess beendet)
# =============================================================================


@pytest.mark.django_db
def test_erstversand_merkt_sich_den_anspruch(org: Any, make_member: Any) -> None:
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org)
    jetzt = MONTAG_18 - timedelta(hours=24)

    assert dispatch_invitations_once(meeting, now=jetzt) == 1

    meeting.refresh_from_db()
    assert meeting.invitation_claimed_at == jetzt
    assert meeting.invitation_sent_at is not None


@pytest.mark.django_db
def test_haengender_anspruch_wird_nach_frist_freigegeben_und_erneut_versendet(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, ["faction.view_public"], email="mitglied@example.org")
    meeting = _meeting(org)
    jetzt = MONTAG_18 - timedelta(hours=24)
    # Prozess nach dem Anspruch beendet: versandt markiert, aber nie abgeschlossen
    FactionMeeting.objects.filter(pk=meeting.pk).update(
        invitation_sent=True, invitation_claimed_at=jetzt - timedelta(minutes=31)
    )

    mail.outbox = []
    stats = run_faction_invitation_pass(now=jetzt)

    meeting.refresh_from_db()
    assert stats["released_claims"] == 1
    assert stats["dispatched"] == 1
    assert [m.to for m in mail.outbox] == [[mitglied.user.email]]
    assert meeting.invitation_sent is True
    assert meeting.invitation_sent_at is not None
    assert meeting.status == "invited"


@pytest.mark.django_db
def test_junger_oder_alter_anspruch_ohne_zeitpunkt_bleibt_stehen(org: Any, make_member: Any) -> None:
    make_member(org, ["faction.view_public"], email="mitglied@example.org")
    laeuft = _meeting(org)
    altbestand = _meeting(org)
    jetzt = MONTAG_18 - timedelta(hours=24)
    # Ein Versand läuft gerade noch; ein Anspruch aus einer Version ohne Zeitpunkt wird nicht angefasst
    FactionMeeting.objects.filter(pk=laeuft.pk).update(
        invitation_sent=True, invitation_claimed_at=jetzt - timedelta(minutes=29)
    )
    FactionMeeting.objects.filter(pk=altbestand.pk).update(invitation_sent=True)

    mail.outbox = []
    stats = run_faction_invitation_pass(now=jetzt)

    assert stats["released_claims"] == 0
    assert mail.outbox == []
    assert set(FactionMeeting.objects.values_list("invitation_sent", flat=True)) == {True}
