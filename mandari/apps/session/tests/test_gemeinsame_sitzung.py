# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Sitzung mehrerer Gremien (Issue #317, Teil B).

Wer mehreren beteiligten Gremien angehört, erhält eine Ladung, eine Anwesenheitszeile und hat eine
Stimme. Weitere Gremien nur aus demselben Mandanten; Anzeige, Gremienfilter, Kalender und OParl
kennen alle beteiligten Gremien.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core import mail
from django.db import transaction
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import (
    attendance_service,
    calendar_service,
    invitation_service,
    joint_meeting_service,
    voting_service,
)

pytestmark = pytest.mark.django_db


@dataclass
class Welt:
    tenant: SessionTenant
    bau: SessionOrganization
    umwelt: SessionOrganization
    meeting: SessionMeeting
    doppelt: SessionPerson
    nur_bau: SessionPerson
    nur_umwelt: SessionPerson
    beratend: SessionPerson
    staff: SessionUser
    client: Client


def _person(tenant: SessionTenant, given: str, family: str) -> SessionPerson:
    return SessionPerson.objects.create(
        tenant=tenant, given_name=given, family_name=family, email=f"{given.lower()}@example.org"
    )


def _sitz(org: SessionOrganization, person: SessionPerson, **extra: Any) -> None:
    SessionOrganizationMembership.objects.create(organization=org, person=person, **extra)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", invitation_period_days=7)
    umwelt = SessionOrganization.objects.create(tenant=tenant, name="Umweltausschuss", invitation_period_days=10)
    doppelt = _person(tenant, "Dora", "Doppel")
    nur_bau = _person(tenant, "Bernd", "Bau")
    nur_umwelt = _person(tenant, "Ulla", "Umwelt")
    beratend = _person(tenant, "Rita", "Rat")
    _sitz(bau, doppelt, role="chair")
    _sitz(umwelt, doppelt, role="member")
    _sitz(bau, nur_bau, role="member")
    _sitz(umwelt, nur_umwelt, role="member")
    # Im federführenden Gremium nur beratend, im weiteren Gremium stimmberechtigt: eine Stimme
    _sitz(bau, beratend, role="advisor", has_voting_rights=False)
    _sitz(umwelt, beratend, role="member")

    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        organization=bau,
        name="Gemeinsame Sitzung Bau und Umwelt",
        start=(timezone.now() + timedelta(days=20)).replace(microsecond=0),
        meeting_state="scheduled",
    )
    meeting.joint_organizations.add(umwelt)

    role = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_create_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_manage_attendance=True,
    )
    user = cast(Any, UserFactory)(email="sitzungsdienst@nord.example")
    staff = SessionUser.objects.create(user=user, tenant=tenant)
    staff.roles.add(role)
    client = Client()
    client.force_login(user)
    return Welt(tenant, bau, umwelt, meeting, doppelt, nur_bau, nur_umwelt, beratend, staff, client)


def _frisch(meeting: SessionMeeting) -> SessionMeeting:
    return SessionMeeting.objects.get(pk=meeting.pk)


# =============================================================================
# Ladung: eine Einladung je Person
# =============================================================================


def test_doppelmitglied_erhaelt_nur_eine_einladung(welt: Welt) -> None:
    empfaenger = invitation_service.get_recipients(_frisch(welt.meeting))
    personen = [r["person"].pk for r in empfaenger]
    assert sorted(personen) == sorted(p.pk for p in (welt.doppelt, welt.nur_bau, welt.nur_umwelt, welt.beratend))
    assert len(personen) == len(set(personen))
    doppelt = next(r for r in empfaenger if r["person"] == welt.doppelt)
    # Funktion aus dem federführenden Gremium, beide Gremien genannt
    assert doppelt["membership"].organization == welt.bau
    assert doppelt["role"] == "Vorsitzende/r"
    assert doppelt["organizations"] == ["Bauausschuss", "Umweltausschuss"]

    mail.outbox = []
    dispatch = invitation_service.send_invitations(_frisch(welt.meeting), sent_by=welt.staff)
    zeilen = SessionInvitationRecipient.objects.filter(dispatch=dispatch)
    assert zeilen.count() == 4
    assert zeilen.filter(person=welt.doppelt).count() == 1
    assert len(mail.outbox) == 4
    assert len([m for m in mail.outbox if "dora@example.org" in m.to]) == 1


def test_gast_in_einem_gremium_mitglied_im_anderen_erhaelt_volle_tagesordnung(welt: Welt) -> None:
    gast = _person(welt.tenant, "Gerd", "Gast")
    _sitz(welt.bau, gast, role="guest", has_voting_rights=False)
    _sitz(welt.umwelt, gast, role="member")
    empfaenger = {r["person"].pk: r for r in invitation_service.get_recipients(_frisch(welt.meeting))}
    assert empfaenger[gast.pk]["include_non_public"] is True


# =============================================================================
# Anwesenheit und Stimmrecht: eine Stimme je Person
# =============================================================================


def test_doppelmitglied_hat_nur_eine_stimme(welt: Welt) -> None:
    meeting = _frisch(welt.meeting)
    assert attendance_service.generate_attendance(meeting) == 4
    assert attendance_service.generate_attendance(meeting) == 0  # erneut: nichts doppelt
    assert SessionAttendance.objects.filter(meeting=meeting, person=welt.doppelt).count() == 1
    beratend = SessionAttendance.objects.get(meeting=meeting, person=welt.beratend)
    assert beratend.has_voting_rights is True  # Stimmrecht aus dem weiteren Gremium
    assert SessionAttendance.objects.get(meeting=meeting, person=welt.doppelt).role == "chair"

    SessionAttendance.objects.filter(meeting=meeting).update(status="present")
    beurteilt = voting_service.eligibility(meeting)
    assert len(beurteilt.voting) == 4
    assert beurteilt.complete is True

    top = SessionAgendaItem.objects.create(meeting=meeting, number="1", order=1, name="Radweg", voting_method="open")
    stimmen = {welt.doppelt: "yes", welt.nur_bau: "yes", welt.nur_umwelt: "no", welt.beratend: "abstain"}
    ergebnis = voting_service.capture_votes(top, stimmen, recorded_by=welt.staff)
    assert (ergebnis["yes"], ergebnis["no"], ergebnis["abstain"]) == (2, 1, 1)
    # Eine zweite Stimme derselben Person überschreibt nur – es bleibt eine Zeile
    voting_service.capture_votes(top, {welt.doppelt: "no"}, recorded_by=welt.staff)
    assert SessionVote.objects.filter(agenda_item=top, person=welt.doppelt).count() == 1
    top.refresh_from_db()
    assert (top.votes_yes, top.votes_no, top.votes_abstain) == (1, 2, 1)

    # Summen über der Zahl der stimmberechtigten Anwesenden (vier Personen, nicht fünf Sitze)
    pruefung = voting_service.check_counts(top, 3, 2, 0, assessed=beurteilt)
    assert pruefung.exceeded and pruefung.hard


# =============================================================================
# Zuordnung: nur eigener Mandant, nie das federführende Gremium, im Protokoll
# =============================================================================


def test_fremdes_und_federfuehrendes_gremium_werden_abgewiesen(welt: Welt) -> None:
    fremd_tenant = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
    fremd = SessionOrganization.objects.create(tenant=fremd_tenant, name="Ausschuss Süd")
    # Der Schutz greift beim Zuordnen selbst, nicht nur im Formular (Savepoint je Versuch)
    with pytest.raises(joint_meeting_service.JointOrganizationError), transaction.atomic():
        welt.meeting.joint_organizations.add(fremd)
    with pytest.raises(joint_meeting_service.JointOrganizationError), transaction.atomic():
        welt.meeting.joint_organizations.add(welt.bau)
    with pytest.raises(joint_meeting_service.JointOrganizationError), transaction.atomic():
        fremd.joint_meetings.add(welt.meeting)
    assert list(welt.meeting.joint_organizations.all()) == [welt.umwelt]


def test_zuordnung_steht_im_protokoll_und_aendert_updated_at(welt: Welt) -> None:
    jugend = SessionOrganization.objects.create(tenant=welt.tenant, name="Jugendhilfeausschuss")
    vorher = _frisch(welt.meeting).updated_at
    welt.meeting.joint_organizations.add(jugend)
    eintrag = SessionAuditLog.objects.filter(tenant=welt.tenant, model_name="SessionMeeting", action="update").first()
    assert eintrag is not None
    assert eintrag.changes == {"weitere_gremien": {"hinzugefuegt": ["Jugendhilfeausschuss"]}}
    assert _frisch(welt.meeting).updated_at >= vorher
    welt.meeting.joint_organizations.clear()
    entfernt = SessionAuditLog.objects.filter(
        tenant=welt.tenant, model_name="SessionMeeting", changes__weitere_gremien__has_key="entfernt"
    )
    assert entfernt.exists()


def test_ladungsfrist_ist_die_laengste_der_gremien(welt: Welt) -> None:
    meeting = _frisch(welt.meeting)
    assert meeting.invitation_period_days == 10
    assert meeting.invitation_deadline == (timezone.localtime(meeting.start) - timedelta(days=10)).date()
    assert meeting.organizations_label == "Bauausschuss und Umweltausschuss"


# =============================================================================
# Formular, Anzeige, Filter, Kalender, OParl
# =============================================================================


def test_formular_legt_gemeinsame_sitzung_an_und_prueft(welt: Welt) -> None:
    fremd_tenant = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
    fremd = SessionOrganization.objects.create(tenant=fremd_tenant, name="Ausschuss Süd")
    daten = {
        "name": "Neue gemeinsame Sitzung",
        "organization": str(welt.umwelt.pk),
        "joint_organizations": [str(welt.bau.pk)],
        "start": "2031-03-01T17:00",
        "is_public": "on",
    }
    antwort = welt.client.post("/session/nord/meetings/create/", daten)
    assert antwort.status_code == 302
    neu = SessionMeeting.objects.get(name="Neue gemeinsame Sitzung")
    assert list(neu.joint_organizations.all()) == [welt.bau]

    # Federführendes Gremium zugleich als weiteres Gremium: Fehler im Formular
    fehler = welt.client.post("/session/nord/meetings/create/", {**daten, "joint_organizations": [str(welt.umwelt.pk)]})
    assert fehler.status_code == 200
    assert "federführende Gremium ist bereits beteiligt" in fehler.content.decode()
    # Gremium eines anderen Mandanten ist nicht wählbar
    fremd_post = welt.client.post("/session/nord/meetings/create/", {**daten, "joint_organizations": [str(fremd.pk)]})
    assert fremd_post.status_code == 200
    assert SessionMeeting.objects.filter(name="Neue gemeinsame Sitzung").count() == 1


def test_anzeige_filter_und_kalender_kennen_alle_gremien(welt: Welt) -> None:
    detail = welt.client.get(f"/session/nord/meetings/{welt.meeting.pk}/").content.decode()
    assert "Gemeinsame Sitzung" in detail and "Umweltausschuss" in detail

    liste = welt.client.get(f"/session/nord/meetings/?organization={welt.umwelt.pk}").content.decode()
    assert "Gemeinsame Sitzung Bau und Umwelt" in liste
    andere = SessionOrganization.objects.create(tenant=welt.tenant, name="Sportausschuss")
    leer = welt.client.get(f"/session/nord/meetings/?organization={andere.pk}").content.decode()
    assert "Gemeinsame Sitzung Bau und Umwelt" not in leer

    suche = welt.client.get(f"/session/nord/search/?q=Gemeinsame&organization={welt.umwelt.pk}").content.decode()
    assert "Gemeinsame Sitzung Bau und Umwelt" in suche

    start = timezone.localtime(welt.meeting.start)
    wochen, anzahl = calendar_service.month_grid(welt.tenant, start.year, start.month, include_non_public=True)
    assert anzahl == 1
    termine = [m for woche in wochen for tag in woche for m in tag["meetings"]]
    assert termine[0].organizations_label == "Bauausschuss und Umweltausschuss"
    jahr = calendar_service.year_meetings(welt.tenant, start.year, organization=welt.umwelt, include_non_public=True)
    assert [m.pk for monat in jahr for m in monat["meetings"]] == [welt.meeting.pk]
    feed = calendar_service.build_organization_feed(welt.umwelt).decode()
    assert "Gemeinsame Sitzung der Gremien Bauausschuss und Umweltausschuss" in feed


def test_oparl_meeting_nennt_alle_gremien(welt: Welt) -> None:
    antwort = Client().get(f"/session/nord/api/oparl/meeting/{welt.meeting.pk}/")
    assert antwort.status_code == 200
    gremien = antwort.json()["organization"]
    assert len(gremien) == 2
    assert gremien[0].endswith(f"/organization/{welt.bau.pk}/")
    assert gremien[1].endswith(f"/organization/{welt.umwelt.pk}/")

    liste = Client().get("/session/nord/api/oparl/meetings/").json()
    assert [len(eintrag["organization"]) for eintrag in liste["data"]] == [2]
