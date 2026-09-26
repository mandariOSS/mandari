# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niemand vergibt mehr Rechte, als er selbst hat; Administrator-Rechte nur durch Administratoren.

Die Invariante gilt auf allen Wegen, über die Rollen und Rechte entstehen: Einladungen (auch
beim Annehmen älterer Einladungen), Standardrolle der Selbstregistrierung, Rollenverwaltung
(``is_admin``, Berechtigungen, eigene Rolle, Zurücksetzen), Rollenvergabe im Mitglieder-Detail
und Änderungsanträge. Entfernen und Deaktivieren verlangen ``members.remove``, Rollenvergabe
``members.manage_roles``; Administratoren bearbeiten nur Administratoren, und mindestens ein
Administrator bleibt.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.common.tests.factories import MembershipFactory, PermissionFactory, RoleFactory, UserFactory
from apps.tenants.models import Membership, Role, UserInvitation
from apps.work.organization import services
from apps.work.organization.models import MemberChangeRequest
from apps.work.organization.services import ServiceError

VORSITZ = [
    "members.view",
    "members.view_details",
    "members.invite",
    "members.edit",
    "members.manage_roles",
    "organization.view",
    "organization.edit",
    "dashboard.view",
]
GESCHAEFTSFUEHRUNG = [
    "members.view",
    "members.view_details",
    "members.edit",
    "members.remove",
    "members.manage_roles",
    "organization.view",
    "organization.manage_roles",
    "dashboard.view",
]


def _rolle(org: Any, name: str, rechte: list[str]) -> Role:
    return cast(Role, RoleFactory(organization=org, name=name, permissions=rechte))  # type: ignore[no-untyped-call]


def _konto(email: str) -> User:
    return cast(User, UserFactory(email=email))  # type: ignore[no-untyped-call]


@pytest.fixture
def rollen(org: Any) -> dict[str, Role]:
    # Die Standardrollen legt ein Signal beim Anlegen der Organisation an
    admin_rolle = Role.objects.get(organization=org, name="Administrator")
    assert admin_rolle.is_admin
    return {
        "admin": admin_rolle,
        "beisitz": _rolle(org, "Beisitz", ["dashboard.view"]),
        "maechtig": _rolle(org, "Vertraulich", ["dashboard.view", "faction.view_non_public"]),
    }


def _mitglied(org: Any, email: str, *rollen: Role) -> Membership:
    membership = MembershipFactory(user=_konto(email), organization=org, roles=list(rollen))  # type: ignore[no-untyped-call]
    return cast(Membership, membership)


@pytest.fixture
def admin(org: Any, rollen: dict[str, Role]) -> Membership:
    return _mitglied(org, "admin@example.org", rollen["admin"])


@pytest.fixture
def vorsitz(org: Any, make_member: Any, admin: Membership) -> Membership:
    return cast(Membership, make_member(org, VORSITZ, email="vorsitz@example.org"))


@pytest.fixture
def gf(org: Any, admin: Membership) -> Membership:
    return _mitglied(org, "gf@example.org", _rolle(org, "Geschäftsstelle", GESCHAEFTSFUEHRUNG))


@pytest.fixture
def mitglied(org: Any, rollen: dict[str, Role]) -> Membership:
    return _mitglied(org, "mitglied@example.org", rollen["beisitz"])


def _codes(membership: Membership) -> set[str]:
    return {role.name for role in membership.roles.all()}


# ---------------------------------------------------------------------------
# Einladungen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("rolle", ["admin", "maechtig"])
def test_einladung_nur_mit_rollen_im_eigenen_rahmen(
    org: Any, vorsitz: Membership, rollen: dict[str, Role], rolle: str
) -> None:
    with pytest.raises(ServiceError):
        services.invite_member(org, vorsitz.user, "neu@example.org", [str(rollen[rolle].id)], "")

    assert not UserInvitation.objects.filter(organization=org, email="neu@example.org").exists()


@pytest.mark.django_db
def test_einladung_mit_rolle_im_eigenen_rahmen_und_durch_admin(
    org: Any, vorsitz: Membership, admin: Membership, rollen: dict[str, Role]
) -> None:
    services.invite_member(org, vorsitz.user, "neu@example.org", [str(rollen["beisitz"].id)], "")
    services.invite_member(org, admin.user, "admin2@example.org", [str(rollen["admin"].id)], "")

    assert UserInvitation.objects.get(email="neu@example.org").roles.get() == rollen["beisitz"]
    assert UserInvitation.objects.get(email="admin2@example.org").roles.get() == rollen["admin"]


@pytest.mark.django_db
def test_offene_einladung_eines_nicht_admins_verleiht_keine_admin_rolle(
    org: Any, vorsitz: Membership, admin: Membership, rollen: dict[str, Role]
) -> None:
    # Vor der Härtung angelegt: Einladung mit Administrator-Rolle durch ein Nicht-Admin-Mitglied
    alt = UserInvitation.create_for_organization(
        organization=org, email="zweitkonto@example.org", invited_by=vorsitz.user, roles=[rollen["admin"]]
    )
    legitim = UserInvitation.create_for_organization(
        organization=org, email="neue-admin@example.org", invited_by=admin.user, roles=[rollen["admin"]]
    )

    services.accept_invitation(alt, _konto("zweitkonto@example.org"))
    services.accept_invitation(legitim, _konto("neue-admin@example.org"))

    assert "Administrator" not in _codes(Membership.objects.get(user__email="zweitkonto@example.org"))
    assert "Administrator" in _codes(Membership.objects.get(user__email="neue-admin@example.org"))


@pytest.mark.django_db
def test_erneute_einladung_reaktiviert_keinen_admin_durch_nicht_admins(
    org: Any, vorsitz: Membership, rollen: dict[str, Role]
) -> None:
    ehemalig = _mitglied(org, "ehemalig@example.org", rollen["admin"])
    ehemalig.is_active = False
    ehemalig.save()

    with pytest.raises(ServiceError):
        services.invite_member(org, vorsitz.user, "ehemalig@example.org", [], "")

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is False


# ---------------------------------------------------------------------------
# Selbstregistrierung
# ---------------------------------------------------------------------------


def _registrierung(client: Any, org: Any, role: Role) -> None:
    client.post(
        reverse("work:organization_registration", kwargs={"org_slug": org.slug}),
        {"registration_enabled": "on", "registration_auto_approve": "on", "registration_default_role": str(role.id)},
    )


@pytest.mark.django_db
def test_standardrolle_der_registrierung_nie_admin_und_im_eigenen_rahmen(
    org: Any, vorsitz: Membership, admin: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    _registrierung(client_for(vorsitz.user), org, rollen["admin"])
    org.refresh_from_db()
    assert org.registration_default_role is None

    _registrierung(client_for(vorsitz.user), org, rollen["maechtig"])
    org.refresh_from_db()
    assert org.registration_default_role is None

    # Auch Administratoren machen die Admin-Rolle nicht zur Standardrolle
    _registrierung(client_for(admin.user), org, rollen["admin"])
    org.refresh_from_db()
    assert org.registration_default_role is None

    _registrierung(client_for(vorsitz.user), org, rollen["beisitz"])
    org.refresh_from_db()
    assert org.registration_default_role == rollen["beisitz"]


@pytest.mark.django_db
def test_selbstregistrierung_vergibt_keine_admin_rolle(org: Any, rollen: dict[str, Role]) -> None:
    org.registration_enabled = True
    org.registration_auto_approve = True
    org.registration_default_role = rollen["admin"]  # Altbestand
    org.save()

    membership = services.join_by_self_registration(org, _konto("selbst@example.org"))

    assert not membership.roles.filter(is_admin=True).exists()


# ---------------------------------------------------------------------------
# Rollenverwaltung
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rollenverwaltung_ohne_admin_setzt_kein_is_admin(org: Any, gf: Membership, client_for: Any) -> None:
    client = client_for(gf.user)
    client.post(reverse("work:role_create", kwargs={"org_slug": org.slug}), {"name": "Neu", "is_admin": "on"})
    beisitz = _rolle(org, "Kasse", ["dashboard.view"])
    client.post(
        reverse("work:role_edit", kwargs={"org_slug": org.slug, "role_id": beisitz.id}),
        {"name": "Kasse", "is_admin": "on", "permissions": ["dashboard.view"]},
    )

    assert not Role.objects.filter(organization=org, name="Neu", is_admin=True).exists()
    beisitz.refresh_from_db()
    assert beisitz.is_admin is False


@pytest.mark.django_db
def test_rollenverwaltung_nur_mit_eigenen_rechten_und_nicht_an_der_eigenen_rolle(
    org: Any, gf: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    PermissionFactory(codename="faction.view_non_public")  # type: ignore[no-untyped-call]
    client = client_for(gf.user)
    eigene = gf.roles.get()
    beisitz = rollen["beisitz"]

    client.post(
        reverse("work:role_edit", kwargs={"org_slug": org.slug, "role_id": beisitz.id}),
        {"name": "Beisitz", "permissions": ["dashboard.view", "faction.view_non_public"]},
    )
    client.post(
        reverse("work:role_edit", kwargs={"org_slug": org.slug, "role_id": eigene.id}),
        {"name": eigene.name, "permissions": [*GESCHAEFTSFUEHRUNG, "faction.view_non_public"]},
    )
    client.post(
        reverse("work:role_create", kwargs={"org_slug": org.slug}),
        {"name": "Hintertür", "permissions": ["faction.view_non_public"]},
    )

    assert set(beisitz.permissions.values_list("codename", flat=True)) == {"dashboard.view"}
    assert "faction.view_non_public" not in set(eigene.permissions.values_list("codename", flat=True))
    assert not Role.objects.filter(organization=org, name="Hintertür").exists()

    # Im eigenen Rahmen bleibt die Rollenverwaltung möglich
    client.post(
        reverse("work:role_edit", kwargs={"org_slug": org.slug, "role_id": beisitz.id}),
        {"name": "Beisitz", "permissions": ["dashboard.view", "members.view"]},
    )
    assert set(beisitz.permissions.values_list("codename", flat=True)) == {"dashboard.view", "members.view"}


@pytest.mark.django_db
def test_zuruecksetzen_macht_keine_rolle_zur_admin_rolle(
    org: Any, gf: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    # Eine Rolle trägt den Namen der Standard-Adminrolle, dann „auf Standard zurücksetzen“
    rollen["admin"].delete()
    rolle = _rolle(org, "Administrator", ["dashboard.view"])

    client_for(gf.user).post(reverse("work:role_reset", kwargs={"org_slug": org.slug, "role_id": rolle.id}))

    rolle.refresh_from_db()
    assert rolle.is_admin is False


# ---------------------------------------------------------------------------
# Mitglieder-Detail
# ---------------------------------------------------------------------------


def _member_action(client: Any, org: Any, member: Membership, action: str, **data: Any) -> None:
    client.post(
        reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": member.id}),
        {"action": action, **data},
    )


@pytest.mark.django_db
def test_entfernen_und_deaktivieren_verlangen_members_remove(
    org: Any, vorsitz: Membership, mitglied: Membership, client_for: Any
) -> None:
    client = client_for(vorsitz.user)

    _member_action(client, org, mitglied, "deactivate")
    _member_action(client, org, mitglied, "remove")

    mitglied.refresh_from_db()
    assert mitglied.is_active is True


@pytest.mark.django_db
def test_administratoren_nur_durch_administratoren_entfernbar(
    org: Any, gf: Membership, admin: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    zweiter_admin = _mitglied(org, "admin-zwei@example.org", rollen["admin"])
    client = client_for(gf.user)

    _member_action(client, org, zweiter_admin, "deactivate")
    _member_action(client, org, admin, "remove")

    zweiter_admin.refresh_from_db()
    assert zweiter_admin.is_active is True
    assert Membership.objects.filter(id=admin.id).exists()


@pytest.mark.django_db
def test_rollenvergabe_verlangt_manage_roles_und_bleibt_im_eigenen_rahmen(
    org: Any, make_member: Any, vorsitz: Membership, mitglied: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    nur_bearbeiten = make_member(org, ["members.view", "members.edit"], email="bearbeiten@example.org")
    ziel = [str(rollen["beisitz"].id), str(rollen["maechtig"].id)]

    _member_action(client_for(nur_bearbeiten.user), org, mitglied, "update_roles", roles=[str(rollen["beisitz"].id)])
    _member_action(client_for(vorsitz.user), org, mitglied, "update_roles", roles=ziel)

    assert _codes(mitglied) == {"Beisitz"}
    with pytest.raises(ServiceError):
        services.update_member_roles(org, mitglied, vorsitz, ziel)


@pytest.mark.django_db
def test_letzter_admin_bleibt_admin(org: Any, admin: Membership, rollen: dict[str, Role]) -> None:
    with pytest.raises(ServiceError):
        services.update_member_roles(org, admin, admin, [str(rollen["beisitz"].id)])

    assert "Administrator" in _codes(admin)


@pytest.mark.django_db
def test_nicht_admins_reaktivieren_keine_administratoren(
    org: Any, gf: Membership, rollen: dict[str, Role], client_for: Any
) -> None:
    ehemalig = _mitglied(org, "ehemalig@example.org", rollen["admin"])
    ehemalig.is_active = False
    ehemalig.save()

    _member_action(client_for(gf.user), org, ehemalig, "reactivate")

    ehemalig.refresh_from_db()
    assert ehemalig.is_active is False


# ---------------------------------------------------------------------------
# Änderungsanträge
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_aenderungsantrag_nur_im_rahmen_der_genehmigenden(
    org: Any, vorsitz: Membership, mitglied: Membership, rollen: dict[str, Role]
) -> None:
    antrag = MemberChangeRequest.objects.create(
        organization=org,
        requester=mitglied,
        request_type="role_change",
        request_data={"requested_roles": [str(rollen["beisitz"].id), str(rollen["maechtig"].id)]},
        reason="Bitte",
    )

    with pytest.raises(ServiceError):
        services.approve_change_request(org, vorsitz, antrag.id)

    assert _codes(mitglied) == {"Beisitz"}
