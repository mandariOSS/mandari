# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldung zur Ladung im Work-Portal (Issue #225).

Die Zuordnung Work-Konto ↔ Person im Sitzungsdienst gilt nur bei bestätigter, eindeutig
gleicher E-Mail-Adresse und aktiver Verbindung Fraktion ↔ Verwaltung. Alles andere ordnet
nichts zu; fremde Ladungen bleiben unsichtbar.
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

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory
from apps.session.models import (
    SessionAPIToken,
    SessionAttendance,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import invitation_service, portal_link_service
from apps.work.motions.ris_submission import connect_with_token

pytestmark = pytest.mark.django_db

EMAIL = "ratsmitglied@example.org"


@dataclass
class Portal:
    tenant: SessionTenant
    meeting: SessionMeeting
    person: SessionPerson
    org: Any
    user: Any
    client: Client
    token: SessionAPIToken


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


@pytest.fixture
def portal() -> Portal:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    committee = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    person = SessionPerson.objects.create(tenant=tenant, given_name="Rita", family_name="Rat", email=EMAIL)
    SessionOrganizationMembership.objects.create(organization=committee, person=person, role="member")
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        name="Sitzung des Hauptausschusses",
        organization=committee,
        start=timezone.now() + timedelta(days=10),
        meeting_state="scheduled",
    )

    org = cast(Any, OrganizationFactory)(name="Fraktion Test", slug="fraktion-test")
    user = cast(Any, UserFactory)(email=EMAIL.upper(), email_verified=True)
    role = cast(Any, RoleFactory)(organization=org, permissions=["meetings.view"])
    membership = cast(Any, MembershipFactory)(user=user, organization=org, roles=[role])
    token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    connect_with_token(org, raw, membership)

    client = Client()
    client.force_login(user)
    return Portal(tenant, meeting, person, org, user, client, token)


def _versenden(portal: Portal) -> SessionInvitationRecipient:
    mail.outbox = []
    dispatch = invitation_service.send_invitations(portal.meeting, sent_by=None)
    return SessionInvitationRecipient.objects.get(dispatch=dispatch, person=portal.person)


def _url(portal: Portal, suffix: str = "") -> str:
    return f"/work/{portal.org.slug}/meetings/ladungen/{suffix}"


def test_zuordnung_nur_bei_bestaetigter_eindeutiger_adresse(portal: Portal) -> None:
    assert portal_link_service.person_for_user(portal.user, portal.org) == portal.person

    portal.user.email_verified = False
    portal.user.save()
    assert portal_link_service.person_for_user(portal.user, portal.org) is None

    portal.user.email_verified = True
    portal.user.save()
    SessionPerson.objects.create(tenant=portal.tenant, given_name="Doppelt", family_name="Adresse", email=EMAIL)
    assert portal_link_service.person_for_user(portal.user, portal.org) is None, "mehrdeutige Adresse"


def test_ohne_gueltige_verbindung_keine_zuordnung(portal: Portal) -> None:
    portal.token.is_active = False
    portal.token.save()

    assert portal_link_service.tenant_for_organization(portal.org) is None
    assert portal_link_service.person_for_user(portal.user, portal.org) is None
    assert "Keine Verbindung zur Verwaltung" in portal.client.get(_url(portal)).content.decode()


def test_ladung_im_portal_bestaetigen_und_absagen(portal: Portal) -> None:
    recipient = _versenden(portal)

    html = portal.client.get(_url(portal)).content.decode()
    assert "Sitzung des Hauptausschusses" in html
    recipient.refresh_from_db()
    assert recipient.acknowledged_at is None, "Anzeigen im Portal bestätigt nichts"

    response = portal.client.post(
        _url(portal, f"{recipient.id}/rueckmeldung/"), {"action": "decline", "reason": "Urlaub"}
    )

    assert response.status_code == 302
    attendance = SessionAttendance.objects.get(meeting=portal.meeting, person=portal.person)
    assert attendance.status == "declined" and attendance.response_source == "portal"
    recipient.refresh_from_db()
    assert recipient.acknowledged_via == "portal"

    pdf = portal.client.get(_url(portal, f"{recipient.id}/tagesordnung.pdf"))
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"


def test_nach_sitzungsbeginn_keine_rueckmeldung_im_portal(portal: Portal) -> None:
    recipient = _versenden(portal)
    SessionMeeting.objects.filter(pk=portal.meeting.pk).update(start=timezone.now() - timedelta(minutes=1))

    response = portal.client.post(_url(portal, f"{recipient.id}/rueckmeldung/"), {"action": "confirm"}, follow=True)

    assert "bereits begonnen" in response.content.decode()
    assert not SessionAttendance.objects.filter(person=portal.person).exists()


def test_fremde_ladungen_bleiben_unsichtbar(portal: Portal) -> None:
    _versenden(portal)
    other = SessionPerson.objects.create(
        tenant=portal.tenant, given_name="Otto", family_name="Other", email="other@example.org"
    )
    SessionOrganizationMembership.objects.create(organization=portal.meeting.organization, person=other)
    dispatch = invitation_service.send_invitations(portal.meeting, sent_by=None, dispatch_type="supplementary")
    fremd = SessionInvitationRecipient.objects.get(dispatch=dispatch, person=other)

    assert portal.client.post(_url(portal, f"{fremd.id}/rueckmeldung/"), {"action": "confirm"}).status_code == 404
    assert portal.client.get(_url(portal, f"{fremd.id}/tagesordnung.pdf")).status_code == 404
    assert not SessionAttendance.objects.filter(person=other).exists()


def test_ohne_zuordnung_kein_zugriff(portal: Portal) -> None:
    recipient = _versenden(portal)
    portal.user.email_verified = False
    portal.user.save()

    assert "Keine Zuordnung zum Sitzungsdienst" in portal.client.get(_url(portal)).content.decode()
    assert portal.client.post(_url(portal, f"{recipient.id}/rueckmeldung/"), {"action": "confirm"}).status_code == 404


def test_zustellweg_portal_nur_mit_portalzugang(portal: Portal) -> None:
    portal.person.delivery_channel = "portal"
    portal.person.save()

    recipient = _versenden(portal)
    assert recipient.channel == "portal"
    assert mail.outbox[0].attachments == [], "Portal-Hinweis ohne Anhänge"
    assert "mandari Work" in mail.outbox[0].body

    portal.token.is_active = False
    portal.token.save()
    recipient = _versenden(portal)
    assert recipient.channel == "email", "ohne Portalzugang Rückfall auf E-Mail"
    assert len(mail.outbox[0].attachments) == 2
