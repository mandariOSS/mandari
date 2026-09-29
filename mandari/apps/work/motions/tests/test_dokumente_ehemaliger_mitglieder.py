# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Private Dokumente ehemaliger Mitglieder für Administration und Fraktionsvorsitz (Issue #590).

Seit dem Entfernen eines Mitglieds Inhalte der Organisation erhalten bleiben (#420), sah private
Dokumente der Person niemand mehr, auch eingereichte Anträge. Das Recht „Dokumente ehemaliger
Mitglieder einsehen“ (``motions.view_former_members``) macht sie lesbar; Standard für Administrator und
Fraktionsvorsitz. Die vollständige Matrix aller Wege steht in ``test_dokument_zugriffsmatrix.py``.
"""

from __future__ import annotations

import importlib
from typing import Any, cast

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from apps.common.permissions import DEFAULT_ROLES, PERMISSION_CATEGORIES, PERMISSIONS
from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import Permission, Role
from apps.work.motions.models import Motion
from apps.work.organization import services as member_services

RECHT = "motions.view_former_members"


def _rechte(rolle: str) -> list[str]:
    return list(cast(list[str], DEFAULT_ROLES[rolle]["permissions"]))


def _mitglied(org: Any, email: str, rolle: str) -> Any:
    """Mitglied mit einer Standardrolle, wie sie beim Anlegen der Organisation entsteht."""
    role = Role.objects.get(organization=org, name=DEFAULT_ROLES[rolle]["name"])
    return MembershipFactory(user=UserFactory(email=email), organization=org, roles=[role])  # type: ignore[no-untyped-call]


@pytest.fixture
def personen(org: Any) -> dict[str, Any]:
    # Katalog und Standardrollen unabhängig vom Datenbankstand: Transaktionale Tests anderer Dateien
    # leeren die Tabellen, die Datenmigrationen gefüllt haben (CI mit pytest-xdist).
    Permission.sync_permissions()  # type: ignore[no-untyped-call]
    Role.create_default_roles(org)  # type: ignore[no-untyped-call]
    return {
        "administrator": _mitglied(org, "admin@example.org", "admin"),
        "vorsitz": _mitglied(org, "vorsitz@example.org", "faction_chair"),
        "stellvertretung": _mitglied(org, "stellv@example.org", "faction_vice_chair"),
        "mitglied": _mitglied(org, "mitglied@example.org", "faction_member"),
        "geht": _mitglied(org, "geht@example.org", "faction_member"),
    }


def _dokument(org: Any, autor: Any, titel: str, visibility: str, status: str) -> Motion:
    motion: Any = Motion(organization=org, author=autor, title=titel, visibility=visibility, status=status)
    motion.set_content_encrypted("<p>Der Rat beschließt …</p>")
    motion.save()
    return cast(Motion, motion)


@pytest.fixture
def dokumente(org: Any, personen: dict[str, Any]) -> dict[str, Motion]:
    """Private Dokumente der Person, die die Organisation verlässt – danach ohne Autor:in."""
    geht = personen["geht"]
    docs = {
        "entwurf": _dokument(org, geht, "Entwurf Radwege", "private", "draft"),
        "eingereicht": _dokument(org, geht, "Antrag Spielplatz", "private", "submitted"),
        "geteilt": _dokument(org, geht, "Anfrage Haushalt", "shared", "draft"),
    }
    member_services.remove_member(org, geht, personen["administrator"].user)
    for motion in docs.values():
        motion.refresh_from_db()
        assert motion.author_id is None
    return docs


def test_recht_im_katalog_und_in_den_standardrollen() -> None:
    assert RECHT in PERMISSIONS
    assert RECHT in PERMISSION_CATEGORIES["motions"]["permissions"]
    assert RECHT in _rechte("admin")
    assert RECHT in _rechte("faction_chair")
    for rolle in DEFAULT_ROLES:
        if rolle not in ("admin", "faction_chair"):
            assert RECHT not in _rechte(rolle), rolle


@pytest.mark.django_db
def test_administration_und_vorsitz_lesen_private_dokumente_ehemaliger(
    personen: dict[str, Any], dokumente: dict[str, Motion]
) -> None:
    for person in ("administrator", "vorsitz"):
        membership = personen[person]
        sichtbar = set(Motion.visible_to(membership).values_list("id", flat=True))  # type: ignore[no-untyped-call]
        for name, motion in dokumente.items():
            assert motion.id in sichtbar, (person, name)
            assert motion.access_level(membership) == "view", (person, name)
            assert not motion.can_edit(membership), (person, name)
            assert not motion.can_delete(membership), (person, name)


@pytest.mark.django_db
def test_andere_sehen_private_dokumente_ehemaliger_nicht(
    personen: dict[str, Any], dokumente: dict[str, Motion]
) -> None:
    for person in ("stellvertretung", "mitglied"):
        membership = personen[person]
        sichtbar = set(Motion.visible_to(membership).values_list("id", flat=True))  # type: ignore[no-untyped-call]
        for name, motion in dokumente.items():
            assert motion.id not in sichtbar, (person, name)
            assert motion.access_level(membership) == "none", (person, name)


@pytest.mark.django_db
def test_verweigertes_recht_greift(personen: dict[str, Any], dokumente: dict[str, Motion]) -> None:
    vorsitz = personen["vorsitz"]
    vorsitz.denied_permissions.add(Permission.objects.get(codename=RECHT))
    vorsitz.__dict__.pop("_permission_checker", None)

    assert dokumente["entwurf"].access_level(vorsitz) == "none"
    assert dokumente["entwurf"].id not in set(Motion.visible_to(vorsitz).values_list("id", flat=True))  # type: ignore[no-untyped-call]


@pytest.mark.django_db
def test_editor_oeffnet_nur_fuer_berechtigte(
    org: Any, personen: dict[str, Any], dokumente: dict[str, Motion], client_for: Any
) -> None:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": dokumente["eingereicht"].id})

    assert client_for(personen["vorsitz"].user).get(url).status_code == 200
    assert client_for(personen["mitglied"].user).get(url).status_code in (302, 403, 404)


@pytest.mark.django_db
def test_hinweis_beim_entfernen(org: Any, personen: dict[str, Any], client_for: Any) -> None:
    url = reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": personen["mitglied"].id})
    html = client_for(personen["administrator"].user).get(url).content.decode()
    assert "Dokumente ehemaliger Mitglieder einsehen" in html


# ---------------------------------------------------------------------------
# Migration tenants/0025
# ---------------------------------------------------------------------------

MIGRATION = importlib.import_module("apps.tenants.migrations.0025_recht_dokumente_ehemaliger_mitglieder")
NACHHER = ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder")
VORHER = MIGRATION.Migration.dependencies[0]


@pytest.mark.django_db(transaction=True)
def test_migration_vergibt_recht_additiv() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Permission = alt.get_model("tenants", "Permission")
        Role = alt.get_model("tenants", "Role")
        # Historisches Modell: ohne die Standardrollen, die das Anlegen sonst per Signal erzeugt
        org = alt.get_model("tenants", "Organization").objects.create(
            name="Fraktion Migration", slug="fraktion-migration"
        )
        Permission.objects.filter(codename=RECHT).delete()  # Stand vor dem neuen Recht
        verwaltung, _ = Permission.objects.get_or_create(
            codename="organization.admin", defaults={"name": "Vollständige Administration", "category": "organization"}
        )
        lesen, _ = Permission.objects.get_or_create(
            codename="motions.view", defaults={"name": "Anträge anzeigen", "category": "motions"}
        )
        administrator = Role.objects.create(organization_id=org.pk, name="Administrator")
        admin_flag = Role.objects.create(organization_id=org.pk, name="Technik", is_admin=True)
        vorsitz = Role.objects.create(organization_id=org.pk, name="Fraktionsvorsitz")
        vorsitz.permissions.add(lesen)
        geschaeftsfuehrung = Role.objects.create(organization_id=org.pk, name="Geschäftsführung")
        geschaeftsfuehrung.permissions.add(verwaltung)
        mitglied = Role.objects.create(organization_id=org.pk, name="Fraktionsmitglied")
        mitglied.permissions.add(lesen)
        stellv = Role.objects.create(organization_id=org.pk, name="Stellv. Vorsitz")

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = executor.loader.project_state([NACHHER]).apps
        Role = neu.get_model("tenants", "Role")

        def rechte(rolle: Any) -> set[str]:
            return set(Role.objects.get(pk=rolle.pk).permissions.values_list("codename", flat=True))

        for rolle in (administrator, admin_flag, vorsitz, geschaeftsfuehrung):
            assert RECHT in rechte(rolle), rolle.name
        for rolle in (mitglied, stellv):
            assert RECHT not in rechte(rolle), rolle.name
        assert rechte(vorsitz) == {"motions.view", RECHT}, "rein additiv"

        # Wiederholbar: ein zweiter Lauf ändert nichts
        MIGRATION.recht_vergeben(neu, None)
        assert rechte(vorsitz) == {"motions.view", RECHT}
        assert rechte(geschaeftsfuehrung) == {"organization.admin", RECHT}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
