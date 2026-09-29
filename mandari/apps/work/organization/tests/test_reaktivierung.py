# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reaktivieren läuft über einen Weg mit denselben Prüfungen.

Eine deaktivierte Mitgliedschaft lässt sich in der Mitgliederverwaltung und über „Mitglied einladen“
wieder aktivieren. Beide Wege nutzen ``_restore_membership``: Gast-Kontingent und die Grenze der
eigenen Rechte gelten gleichermaßen.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Membership
from apps.work.organization import services
from apps.work.organization.services import ServiceError


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.invite", "members.edit"], email="admin@example.org", is_admin=True)


def _gast(org: Any, email: str, *, aktiv: bool) -> Membership:
    user = cast(Any, UserFactory)(email=email)
    return cast(Membership, cast(Any, MembershipFactory)(user=user, organization=org, is_guest=True, is_active=aktiv))


@pytest.mark.django_db
def test_einladen_reaktiviert_gast_nur_mit_freiem_platz(org: Any, admin: Any) -> None:
    org.guest_limit = 1
    org.save(update_fields=["guest_limit"])
    _gast(org, "aktiv@example.org", aktiv=True)
    ehemalig = _gast(org, "ehemalig@example.org", aktiv=False)

    with pytest.raises(ServiceError) as fehler:
        services.invite_member(org, admin.user, "ehemalig@example.org", [], "")

    assert "Gast-Limit" in str(fehler.value)
    ehemalig.refresh_from_db()
    assert ehemalig.is_active is False
    assert org.get_active_guest_count() == 1


@pytest.mark.django_db
def test_einladen_reaktiviert_gast_mit_freiem_platz(org: Any, admin: Any) -> None:
    org.guest_limit = 2
    org.save(update_fields=["guest_limit"])
    _gast(org, "aktiv@example.org", aktiv=True)
    ehemalig = _gast(org, "ehemalig@example.org", aktiv=False)

    services.invite_member(org, admin.user, "ehemalig@example.org", [], "")

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is True
    assert ehemalig.is_guest is True


@pytest.mark.django_db
def test_einladungsformular_haelt_das_kontingent_ein(org: Any, admin: Any, client_for: Any) -> None:
    org.guest_limit = 1
    org.save(update_fields=["guest_limit"])
    _gast(org, "aktiv@example.org", aktiv=True)
    ehemalig = _gast(org, "ehemalig@example.org", aktiv=False)

    client_for(admin.user).post(
        reverse("work:member_invite", kwargs={"org_slug": org.slug}), {"email": "ehemalig@example.org"}
    )

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is False


@pytest.mark.django_db
def test_beide_wege_melden_dasselbe(org: Any, admin: Any) -> None:
    org.guest_limit = 1
    org.save(update_fields=["guest_limit"])
    _gast(org, "aktiv@example.org", aktiv=True)
    ehemalig = _gast(org, "ehemalig@example.org", aktiv=False)

    with pytest.raises(ServiceError) as ueber_einladung:
        services.invite_member(org, admin.user, "ehemalig@example.org", [], "")
    with pytest.raises(ServiceError) as ueber_verwaltung:
        services.reactivate_member(org, ehemalig, admin)

    assert str(ueber_einladung.value) == str(ueber_verwaltung.value)
