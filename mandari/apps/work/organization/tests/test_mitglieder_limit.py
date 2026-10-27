# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitglieder-Limit (``member_limit``, Issue #1132): keine neuen regulären Mitglieder und Einladungen über
dem Limit, bestehende Mitglieder bleiben, leer heißt unbegrenzt. Gäste zählen getrennt (``guest_limit``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Membership, UserInvitation
from apps.work.organization import services
from apps.work.organization.services import ServiceError


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["members.invite", "members.edit", "guests.invite"], email="admin@example.org", is_admin=True
    )


def set_limit(org: Any, limit: int | None) -> None:
    org.member_limit = limit
    org.save(update_fields=["member_limit"])


def pending_request(org: Any, email: str) -> Membership:
    user = cast(Any, UserFactory)(email=email)
    return Membership.objects.create(
        user=user, organization=org, is_active=False, registration_requested_at=timezone.now()
    )


@pytest.mark.django_db
def test_einladung_ueber_dem_limit_wird_abgelehnt(org: Any, admin: Any) -> None:
    set_limit(org, 2)
    services.invite_member(org, admin.user, "erste@example.org", [], "")

    # Ein aktives Mitglied und eine offene Einladung belegen beide Plätze
    with pytest.raises(ServiceError) as excinfo:
        services.invite_member(org, admin.user, "zweite@example.org", [], "")
    assert "Mitglieder-Limit erreicht (2)" in str(excinfo.value)
    assert "offene Einladungen" in str(excinfo.value)
    assert not UserInvitation.objects.filter(organization=org, email="zweite@example.org").exists()

    # Zurückgezogene Einladung gibt den Platz frei
    services.cancel_invitation(UserInvitation.objects.get(organization=org, email="erste@example.org"))
    services.invite_member(org, admin.user, "zweite@example.org", [], "")


@pytest.mark.django_db
def test_ohne_limit_unbegrenzt(org: Any, admin: Any) -> None:
    set_limit(org, None)
    for number in range(5):
        services.invite_member(org, admin.user, f"person-{number}@example.org", [], "")
    assert UserInvitation.objects.filter(organization=org).count() == 5
    assert org.has_free_member_slot(reserved=1000)


@pytest.mark.django_db
def test_annahme_bei_vollem_limit_bleibt_offen(org: Any, admin: Any) -> None:
    invitation = UserInvitation.create_for_organization(
        organization=org, email="neu@example.org", invited_by=admin.user
    )
    set_limit(org, 1)
    user = cast(Any, UserFactory)(email="neu@example.org")

    with pytest.raises(ServiceError) as excinfo:
        services.accept_invitation(invitation, user)
    assert "Mitglieder-Limit" in str(excinfo.value)
    assert not Membership.objects.filter(organization=org, user=user).exists()
    invitation.refresh_from_db()
    assert invitation.accepted_at is None, "die Einladung bleibt gültig"

    set_limit(org, 2)
    services.accept_invitation(invitation, user)
    assert Membership.objects.filter(organization=org, user=user, is_active=True).exists()


@pytest.mark.django_db
def test_reaktivieren_und_freischalten_unterliegen_dem_limit(org: Any, admin: Any) -> None:
    inactive = cast(Any, MembershipFactory)(organization=org, is_active=False)
    request = pending_request(org, "anfrage@example.org")
    set_limit(org, 1)

    with pytest.raises(ServiceError):
        services.reactivate_member(org, inactive, admin)
    with pytest.raises(ServiceError):
        services.invite_member(org, admin.user, inactive.user.email, [], "")
    with pytest.raises(ServiceError):
        services.approve_registration(request, actor=admin)
    inactive.refresh_from_db()
    request.refresh_from_db()
    assert inactive.is_active is False and request.is_active is False


@pytest.mark.django_db
def test_bestehende_mitglieder_bleiben_ueber_dem_limit(org: Any, admin: Any) -> None:
    others = [cast(Any, MembershipFactory)(organization=org) for _ in range(3)]
    set_limit(org, 2)

    assert org.get_active_member_count() == 4
    assert not org.has_free_member_slot()
    for membership in others:
        membership.refresh_from_db()
        assert membership.is_active is True
    # Deaktivieren bleibt möglich, die Zahl sinkt
    services.deactivate_member(org, others[0], admin.user)
    assert org.get_active_member_count() == 3


@pytest.mark.django_db
def test_gaeste_zaehlen_nicht_gegen_das_mitglieder_limit(org: Any, admin: Any) -> None:
    set_limit(org, 1)
    services.invite_guest(
        org, admin, email="gast@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )
    assert Membership.objects.filter(organization=org, is_guest=True, is_active=True).count() == 1


@pytest.mark.django_db
def test_selbstregistrierung_wird_bei_vollem_limit_zur_anfrage(org: Any, admin: Any) -> None:
    org.registration_enabled = True
    org.registration_auto_approve = True
    org.save()
    set_limit(org, 1)
    user = cast(Any, UserFactory)(email="selbst@example.org")

    membership = services.join_by_self_registration(org, user)

    assert membership.is_active is False
    assert membership.registration_requested_at is not None


@pytest.mark.django_db
def test_abgelaufene_einladung_erneut_senden_unterliegt_dem_limit(org: Any, admin: Any) -> None:
    invitation = UserInvitation.create_for_organization(
        organization=org, email="alt@example.org", invited_by=admin.user
    )
    UserInvitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now() - timedelta(days=1))
    invitation.refresh_from_db()
    set_limit(org, 1)

    with pytest.raises(ServiceError):
        services.resend_invitation(org, invitation)
    invitation.refresh_from_db()
    assert invitation.expires_at < timezone.now()


@pytest.mark.django_db
def test_freischalten_in_der_oberflaeche_zeigt_die_meldung(client_for: Any, org: Any, admin: Any) -> None:
    request = pending_request(org, "anfrage@example.org")
    set_limit(org, 1)

    response = client_for(admin.user).post(
        reverse("work:member_approve", kwargs={"org_slug": org.slug, "membership_id": request.id})
    )

    assert response.status_code == 302
    request.refresh_from_db()
    assert request.is_active is False
    assert any("Mitglieder-Limit erreicht" in str(message) for message in get_messages(response.wsgi_request))
    assert User.objects.filter(email="anfrage@example.org").exists()
