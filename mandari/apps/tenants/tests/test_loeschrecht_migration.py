# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migration tenants/0024: Rollen mit „Alle Anträge bearbeiten“ erhalten „Anträge anderer löschen“.

Das Löschen fremder Dokumente verlangt ``motions.delete``. Rollen mit ``motions.edit_all`` behalten
damit ihr bisheriges Verhalten; die Standardrollen mit ``edit_all`` enthalten das Recht ebenfalls.
"""

from __future__ import annotations

import importlib
from typing import Any, cast

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.common.permissions import DEFAULT_ROLES
from apps.common.tests.factories import OrganizationFactory

MIGRATION = importlib.import_module("apps.tenants.migrations.0024_loeschrecht_fuer_leitungsrollen")
NACHHER = ("tenants", "0024_loeschrecht_fuer_leitungsrollen")
VORHER = MIGRATION.Migration.dependencies[0]


def test_standardrollen_mit_edit_all_duerfen_loeschen() -> None:
    for schluessel, rolle in DEFAULT_ROLES.items():
        rechte = set(cast(list[str], rolle.get("permissions", [])))
        if "motions.edit_all" in rechte:
            assert "motions.delete" in rechte, schluessel


@pytest.mark.django_db(transaction=True)
def test_migration_ergaenzt_loeschrecht_nur_bei_edit_all() -> None:
    org: Any = OrganizationFactory(name="Fraktion Migration", slug="fraktion-migration")  # type: ignore[no-untyped-call]
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Permission = alt.get_model("tenants", "Permission")
        Role = alt.get_model("tenants", "Role")
        bearbeiten_alle, _ = Permission.objects.get_or_create(
            codename="motions.edit_all", defaults={"name": "Alle Anträge bearbeiten", "category": "motions"}
        )
        bearbeiten, _ = Permission.objects.get_or_create(
            codename="motions.edit", defaults={"name": "Eigene Anträge bearbeiten", "category": "motions"}
        )
        Permission.objects.filter(codename="motions.delete").delete()  # auch ohne vorhandenes Recht
        leitung = Role.objects.create(organization_id=org.pk, name="Eigene Leitung")
        leitung.permissions.add(bearbeiten_alle)
        mitglied = Role.objects.create(organization_id=org.pk, name="Eigenes Mitglied")
        mitglied.permissions.add(bearbeiten)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = executor.loader.project_state([NACHHER]).apps
        Role = neu.get_model("tenants", "Role")

        def rechte(rolle: Any) -> set[str]:
            return set(Role.objects.get(pk=rolle.pk).permissions.values_list("codename", flat=True))

        assert "motions.delete" in rechte(leitung)
        assert "motions.delete" not in rechte(mitglied)

        # Wiederholbar: ein zweiter Lauf ändert nichts
        MIGRATION.loeschrecht_ergaenzen(neu, None)
        assert rechte(leitung) == {"motions.edit_all", "motions.delete"}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
