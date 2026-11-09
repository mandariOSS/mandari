# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neue Mitglieder in kommenden Fraktionssitzungen (Issue #1288): Nachtragen bei Beitritt, Reaktivierung, neuer Rolle,
neuem Gremium und Auswahl einer Art; Einladung an Nachgetragene im Einladungslauf; „Teilnehmer hinzufügen“ vor der
Sitzung als eingeladen; Befehl für den Bestand.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Role
from apps.work.faction import teilnahme
from apps.work.faction.invitations import run_faction_invitation_pass
from apps.work.faction.models import FactionAttendance, FactionMeeting, FactionMeetingType

pytestmark = pytest.mark.django_db

ANSICHT = ["faction.view_public"]


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, [], email="vorsitz@example.org", is_admin=True)


def _sitzung(org: Any, vorsitz: Any, *, tage: int = 5, **felder: Any) -> FactionMeeting:
    werte = {"status": "planned", **felder}
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=tage),
        created_by=vorsitz,
        **werte,
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="invited")
    return meeting


def _beitreten(org: Any, make_member: Any, capture: Any, email: str = "neu@example.org", **kwargs: Any) -> Any:
    with capture(execute=True):
        return make_member(org, ANSICHT, email=email, **kwargs)


def _eingeladen(meeting: FactionMeeting) -> set[str]:
    return set(meeting.attendances.filter(membership__isnull=False).values_list("membership__user__email", flat=True))


def test_neues_mitglied_steht_in_den_kommenden_sitzungen(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    kommend = _sitzung(org, vorsitz)
    eingeladen = _sitzung(
        org, vorsitz, tage=9, status="invited", invitation_sent=True, invitation_sent_at=timezone.now()
    )
    vorbei = _sitzung(org, vorsitz, tage=-3)
    laufend = _sitzung(org, vorsitz, tage=1, status="ongoing")
    abgesagt = _sitzung(org, vorsitz, tage=2, status="cancelled")

    neu = _beitreten(org, make_member, django_capture_on_commit_callbacks)

    for meeting in (kommend, eingeladen):
        assert neu.user.email in _eingeladen(meeting)
        assert meeting.attendances.get(membership=neu).status == "invited"
    for meeting in (vorbei, laufend, abgesagt):
        assert neu.user.email not in _eingeladen(meeting)
    # Kein zweites Mal
    assert teilnahme.nachtragen(neu) == 0
    assert kommend.attendances.filter(membership=neu).count() == 1


def test_gaeste_und_inaktive_werden_nicht_eingetragen(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    meeting = _sitzung(org, vorsitz)
    # Ohne mitgeschnittene Signale angelegt, danach als Gast gespeichert
    gast = make_member(org, ANSICHT, email="gast@example.org")
    gast.is_guest = True
    with django_capture_on_commit_callbacks(execute=True):
        gast.save()
    assert gast.user.email not in _eingeladen(meeting)

    weg = make_member(org, ANSICHT, email="weg@example.org")
    weg.is_active = False
    with django_capture_on_commit_callbacks(execute=True):
        weg.save()
    assert weg.user.email not in _eingeladen(meeting)

    # Reaktiviert: wieder dabei
    weg.is_active = True
    with django_capture_on_commit_callbacks(execute=True):
        weg.save()
    assert weg.user.email in _eingeladen(meeting)


def test_bestehende_teilnahme_bleibt_wie_sie_ist(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    meeting = _sitzung(org, vorsitz)
    mitglied = make_member(org, ANSICHT, email="mitglied@example.org")
    FactionAttendance.objects.filter(meeting=meeting, membership=mitglied).delete()
    FactionAttendance.objects.create(meeting=meeting, membership=mitglied, status="declined")
    with django_capture_on_commit_callbacks(execute=True):
        mitglied.save()
    assert list(meeting.attendances.filter(membership=mitglied).values_list("status", flat=True)) == ["declined"]


def test_nur_sitzungen_der_eigenen_arten_auch_nach_neuer_rolle(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    kernteam = Role.objects.create(organization=org, name="Kernteam")
    art = FactionMeetingType.objects.create(
        organization=org, name="Kernteam", participants_mode="role", participants_role=kernteam
    )
    auswahl = FactionMeetingType.objects.create(organization=org, name="AG Haushalt", participants_mode="persons")
    kern = _sitzung(org, vorsitz, meeting_type=art)
    ag = _sitzung(org, vorsitz, meeting_type=auswahl)
    alle = _sitzung(org, vorsitz)

    neu = _beitreten(org, make_member, django_capture_on_commit_callbacks)
    assert neu.user.email in _eingeladen(alle)
    assert neu.user.email not in _eingeladen(kern) | _eingeladen(ag)

    # Neue Rolle: die Sitzungen dieser Art kommen dazu (auch über die Gegenrichtung)
    with django_capture_on_commit_callbacks(execute=True):
        neu.roles.add(kernteam)
    assert neu.user.email in _eingeladen(kern)
    # In die Auswahl einer Art aufgenommen
    with django_capture_on_commit_callbacks(execute=True):
        auswahl.participants.add(neu)
    assert neu.user.email in _eingeladen(ag)


def test_einladung_an_nachgetragene_im_einladungslauf(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    bisher = make_member(org, ANSICHT, email="bisher@example.org")
    meeting = _sitzung(
        org,
        vorsitz,
        status="invited",
        invitation_sent=True,
        invitation_sent_at=timezone.now() - timedelta(hours=2),
    )
    # Altbestand: vor dem Versand angelegt, ohne erfasstes Ergebnis – bekommt nichts
    FactionAttendance.objects.create(meeting=meeting, membership=bisher, status="invited")
    meeting.attendances.update(created_at=timezone.now() - timedelta(hours=3))

    neu = _beitreten(org, make_member, django_capture_on_commit_callbacks)
    mail.outbox.clear()
    stats = run_faction_invitation_pass()

    assert stats["late_invitations"] == 1
    assert [nachricht.to for nachricht in mail.outbox] == [[neu.user.email]]
    assert "Aktualisiert" not in mail.outbox[0].subject
    teilnahme_neu = meeting.attendances.get(membership=neu)
    assert teilnahme_neu.invitation_mailed_at is not None and teilnahme_neu.status == "invited"
    meeting.refresh_from_db()
    assert meeting.invitation_sequence == 0

    # Der nächste Lauf schreibt niemanden erneut an
    mail.outbox.clear()
    assert run_faction_invitation_pass()["late_invitations"] == 0
    assert not mail.outbox


def test_nachgetragene_im_opt_out_modus_gelten_als_angemeldet(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    org.settings = {**(org.settings or {}), "faction": {"invitation_mode": "opt_out"}}
    org.save(update_fields=["settings"])
    meeting = _sitzung(
        org,
        vorsitz,
        status="invited",
        invitation_sent=True,
        invitation_sent_at=timezone.now() - timedelta(hours=1),
        rsvp_enabled=True,
    )
    neu = _beitreten(org, make_member, django_capture_on_commit_callbacks)
    teilnahme.nachzuegler_einladen()
    assert meeting.attendances.get(membership=neu).status == "confirmed"


def test_ohne_versand_wartet_die_nachgetragene_einladung_auf_den_erstversand(
    org: Any, vorsitz: Any, make_member: Any, django_capture_on_commit_callbacks: Any
) -> None:
    meeting = _sitzung(org, vorsitz)
    _beitreten(org, make_member, django_capture_on_commit_callbacks)
    mail.outbox.clear()
    assert teilnahme.nachzuegler_einladen() == 0
    assert not mail.outbox
    assert meeting.attendances.count() == 2


def test_teilnehmer_hinzufuegen_vor_der_sitzung_ist_eingeladen(
    org: Any, vorsitz: Any, make_member: Any, client_for: Any
) -> None:
    meeting = _sitzung(org, vorsitz)
    mitglied = make_member(org, ANSICHT, email="mitglied@example.org")
    FactionAttendance.objects.filter(meeting=meeting, membership=mitglied).delete()
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})
    antwort = client_for(vorsitz.user).post(
        url, {"action": "add_attendee", "attendee_type": "member", "membership_id": str(mitglied.pk)}
    )
    assert antwort.status_code == 302
    teilnahme_neu = meeting.attendances.get(membership=mitglied)
    assert teilnahme_neu.status == "invited" and teilnahme_neu.checked_in_at is None

    # Während der Sitzung wie bisher anwesend
    FactionMeeting.objects.filter(pk=meeting.pk).update(status="ongoing")
    spaet = make_member(org, ANSICHT, email="spaet@example.org")
    FactionAttendance.objects.filter(meeting=meeting, membership=spaet).delete()
    client_for(vorsitz.user).post(
        url, {"action": "add_attendee", "attendee_type": "member", "membership_id": str(spaet.pk)}
    )
    assert meeting.attendances.get(membership=spaet).status == "present"


def test_befehl_traegt_den_bestand_nach_erst_im_ernstfall(org: Any, vorsitz: Any, make_member: Any) -> None:
    meeting = _sitzung(org, vorsitz, status="invited", invitation_sent=True, invitation_sent_at=timezone.now())
    # Ohne Signal angelegt (wie vor Issue #1288): fehlt in der Sitzung
    fehlt = make_member(org, ANSICHT, email="fehlt@example.org")
    assert fehlt.user.email not in _eingeladen(meeting)

    ausgabe = StringIO()
    call_command("fraktion_teilnahme_nachtragen", "--org", org.slug, stdout=ausgabe)
    assert "Probelauf, fehlen: 1 Teilnahme(n), davon 1 mit Einladung per E-Mail." in ausgabe.getvalue()
    assert fehlt.user.email not in _eingeladen(meeting)

    ausgabe = StringIO()
    call_command("fraktion_teilnahme_nachtragen", "--org", org.slug, "--ausfuehren", stdout=ausgabe)
    assert "Nachgetragen: 1 Teilnahme(n)" in ausgabe.getvalue()
    assert fehlt.user.email in _eingeladen(meeting)
    # Ausgabe nur mit Zahlen, ohne Namen oder Adressen
    assert "fehlt@example.org" not in ausgabe.getvalue()
