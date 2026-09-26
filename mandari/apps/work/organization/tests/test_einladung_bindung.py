# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einladungen gelten für die eingeladene Adresse und reaktivieren niemanden.

- Nur das Konto mit der eingeladenen E-Mail-Adresse (ohne Beachtung der Groß-/Kleinschreibung)
  kann eine Einladung annehmen.
- Eine deaktivierte Mitgliedschaft wird über eine Einladung nicht wieder aktiv.
- Beim Deaktivieren verfallen offene Einladungen an die Person und von ihr.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.common.tests.factories import UserFactory
from apps.tenants.models import Membership, UserInvitation
from apps.work.organization import services


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.edit", "members.remove"], email="admin@example.org", is_admin=True)


def _konto(email: str) -> Any:
    return UserFactory(email=email)  # type: ignore[no-untyped-call]


def _annehmen(client: Any, invitation: UserInvitation) -> Any:
    return client.post(reverse("work:accept_invitation", kwargs={"token": invitation.token}))


@pytest.mark.django_db
def test_einladung_nur_fuer_die_eingeladene_adresse(org: Any, admin: Any, client_for: Any) -> None:
    invitation = UserInvitation.create_for_organization(
        organization=org, email="Neu@Example.org", invited_by=admin.user
    )
    fremdes_konto = _konto("jemand@example.org")

    _annehmen(client_for(fremdes_konto), invitation)

    invitation.refresh_from_db()
    assert not Membership.objects.filter(organization=org, user=fremdes_konto).exists()
    assert invitation.accepted_at is None

    eingeladen = _konto("neu@example.org")
    _annehmen(client_for(eingeladen), invitation)
    assert Membership.objects.filter(organization=org, user=eingeladen, is_active=True).exists()


@pytest.mark.django_db
def test_einladung_reaktiviert_keine_deaktivierte_mitgliedschaft(
    org: Any, admin: Any, make_member: Any, client_for: Any
) -> None:
    ehemalig = make_member(org, ["dashboard.view"], email="ehemalig@example.org")
    ehemalig.is_active = False
    ehemalig.save()
    invitation = UserInvitation.create_for_organization(
        organization=org, email="ehemalig@example.org", invited_by=admin.user
    )

    _annehmen(client_for(ehemalig.user), invitation)

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is False


@pytest.mark.django_db
def test_deaktivieren_widerruft_offene_einladungen(org: Any, admin: Any, make_member: Any) -> None:
    mitglied = make_member(org, ["members.invite"], email="mitglied@example.org")
    an_person = UserInvitation.create_for_organization(
        organization=org, email="mitglied@example.org", invited_by=admin.user
    )
    von_person = UserInvitation.create_for_organization(
        organization=org, email="wegwerf@example.org", invited_by=mitglied.user
    )
    andere = UserInvitation.create_for_organization(organization=org, email="andere@example.org", invited_by=admin.user)

    services.deactivate_member(org, mitglied, admin.user)

    offen = set(UserInvitation.objects.filter(organization=org).values_list("id", flat=True))
    assert an_person.id not in offen
    assert von_person.id not in offen
    assert andere.id in offen
