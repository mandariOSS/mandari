# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollen, Rechte und Zugänge in der Änderungshistorie der Fraktion (Issue #366).

Die Zuordnung von Rollen und Einzelrechten löst kein ``post_save`` aus; ohne eigenen Eintrag bliebe eine
Rechteausweitung im Work-Portal unsichtbar. Geprüft wird je Weg: Eintrag in der Kette der Organisation,
handelnde Person, Angaben im Format der Historie (alt → neu), kein Eintrag ohne Änderung.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from apps.common import audit_chain
from apps.common.tests.factories import PermissionFactory, UserFactory
from apps.tenants.models import Membership, Role, UserInvitation
from apps.work.faction.models import FactionAuditLog
from apps.work.organization import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.edit"], email="admin@example.org", is_admin=True)


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="mitglied@example.org")


def entries(org: Any, model_name: str) -> list[FactionAuditLog]:
    return list(FactionAuditLog.objects.filter(organization=org, model_name=model_name).order_by("seq"))


def test_rollenaenderung_steht_in_der_aenderungshistorie(org: Any, admin: Any, member: Any) -> None:
    vorher = sorted(role.name for role in member.roles.all())
    beisitz = Role.objects.create(organization=org, name="Beisitz")
    services.update_member_roles(org, member, admin, [str(beisitz.id)])

    (eintrag,) = entries(org, "Membership")
    assert eintrag.action == "update"
    assert eintrag.object_id == member.id
    assert eintrag.object_repr == "Mitgliedschaft mitglied@example.org"
    assert eintrag.changes == {"Rollen": {"alt": ", ".join(vorher), "neu": "Beisitz"}}
    assert eintrag.membership == admin
    assert eintrag.actor_label == admin.user.get_display_name()
    assert eintrag.is_internal is False
    # In der Hash-Kette der Organisation
    assert eintrag.seq is not None
    assert audit_chain.verify(audit_chain.FACTION, org.pk).errors == []

    # Dieselben Rollen noch einmal: kein Eintrag
    services.update_member_roles(org, member, admin, [str(beisitz.id)])
    assert len(entries(org, "Membership")) == 1


def test_einzelrechte_vereidigung_und_zugang(org: Any, admin: Any, member: Any) -> None:
    rollen = ", ".join(sorted(role.name for role in member.roles.all()))
    PermissionFactory(codename="motions.share")  # type: ignore[no-untyped-call]
    services.update_member_permissions(member, admin, ["motions.share"], ["dashboard.view"])
    services.update_sworn_in(member, admin, True)
    services.deactivate_member(org, member, admin.user)
    member.refresh_from_db()
    services.reactivate_member(org, member, admin)
    member.refresh_from_db()
    services.remove_member(org, member, admin.user)

    assert [(e.action, e.changes) for e in entries(org, "Membership")] == [
        (
            "update",
            {
                "Zusätzliche Rechte": {"alt": "", "neu": "motions.share"},
                "Verweigerte Rechte": {"alt": "", "neu": "dashboard.view"},
            },
        ),
        ("update", {"Vereidigt": {"alt": "nein", "neu": "ja"}}),
        ("update", {"Zugang": {"alt": "aktiv", "neu": "deaktiviert"}}),
        ("update", {"Zugang": {"alt": "deaktiviert", "neu": "aktiv"}}),
        ("delete", {"Zugang": {"alt": "aktiv", "neu": "entfernt"}, "Rollen": {"alt": rollen, "neu": ""}}),
    ]
    assert {e.membership for e in entries(org, "Membership")} == {admin}


def test_rollen_aus_einladung_selbstregistrierung_und_antrag(org: Any, admin: Any, member: Any) -> None:
    neu = Role.objects.create(organization=org, name="Neu")
    invitation = UserInvitation.create_for_organization(
        organization=org, email="gast@example.org", invited_by=admin.user, roles=[neu]
    )
    user: Any = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    services.accept_invitation(invitation, user)
    angenommen = Membership.objects.get(organization=org, user=user)

    (eintrag,) = entries(org, "Membership")
    assert eintrag.object_id == angenommen.id
    assert eintrag.changes == {
        "Rollen": {"alt": "", "neu": "Neu"},
        "Anlass": {"alt": "", "neu": "Einladung angenommen (eingeladen von admin@example.org)"},
    }

    # Genehmigter Änderungsantrag
    request = services.submit_change_request(org, member, "role_change", "Bitte", [str(neu.id)])
    services.approve_change_request(org, admin, request.id)
    antrag = entries(org, "Membership")[-1]
    assert antrag.object_id == member.id
    assert antrag.changes["Rollen"]["neu"] == "Neu"
    assert antrag.changes["Anlass"] == {"alt": "", "neu": "Änderungsantrag genehmigt"}


def test_rolle_anlegen_aendern_loeschen(org: Any, admin: Any) -> None:
    for code in ("motions.view", "motions.edit"):
        PermissionFactory(codename=code)  # type: ignore[no-untyped-call]
    role = services.create_role(org, services.RoleInput(name="Beisitz", permission_codes=["motions.view"]), admin)
    services.update_role(
        org, role, services.RoleInput(name="Beisitz", permission_codes=["motions.edit"], color="#112233"), admin
    )
    # Nur die Farbe: keine Rechteänderung, kein Eintrag
    services.update_role(
        org, role, services.RoleInput(name="Beisitz", permission_codes=["motions.edit"], color="#445566"), admin
    )
    services.delete_role(role)

    assert [(e.action, e.object_repr, e.changes) for e in entries(org, "Role")] == [
        (
            "create",
            "Rolle „Beisitz“",
            {
                "Name": {"alt": "", "neu": "Beisitz"},
                "Administrator": {"alt": "", "neu": "nein"},
                "Rechte": {"alt": "", "neu": "motions.view"},
            },
        ),
        (
            "update",
            "Rolle „Beisitz“",
            {
                "Rechte erteilt": {"alt": "", "neu": "motions.edit"},
                "Rechte entzogen": {"alt": "motions.view", "neu": ""},
            },
        ),
        (
            "delete",
            "Rolle „Beisitz“",
            {
                "Name": {"alt": "Beisitz", "neu": ""},
                "Administrator": {"alt": "nein", "neu": ""},
                "Rechte": {"alt": "motions.edit", "neu": ""},
            },
        ),
    ]


def test_eintrag_ueber_die_oberflaeche_mit_handelnder_person(
    org: Any, admin: Any, member: Any, client_for: Any
) -> None:
    """Über die Mitgliederverwaltung: Person und Adresse kommen aus dem Request."""
    beisitz = Role.objects.create(organization=org, name="Beisitz")
    client: Client = client_for(admin.user)
    response = client.post(
        f"/work/{org.slug}/organization/members/{member.id}/",
        {"action": "update_roles", "roles": [str(beisitz.id)]},
        REMOTE_ADDR="192.0.2.10",
    )
    assert response.status_code in (200, 302)
    (eintrag,) = entries(org, "Membership")
    assert eintrag.changes["Rollen"]["neu"] == "Beisitz"
    assert eintrag.membership == admin
    assert eintrag.ip_address == "192.0.2.10"
