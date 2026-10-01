# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Basisadresse der Kennungen (Issue #733): einmal je Installation festgelegt, danach unabhängig von SITE_URL.

Die Migration common/0009 übernimmt den damaligen Wert von ``SITE_URL``; auf einer neuen Installation legt
der erste Bedarf die Basis fest. Spätere Änderungen von ``SITE_URL`` ändern sie nicht.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from apps.common.identifiers import identifier_base, site_url, stored_identifier_base
from apps.common.models import IdentifierBase

MIGRATION = importlib.import_module("apps.common.migrations.0009_kennungsbasis")
VORHER = ("common", "0008_verdeckte_mail_backend_wahl_nur_aus_dem_code")
NACHHER = ("common", "0009_kennungsbasis")


@pytest.mark.django_db
def test_erster_bedarf_legt_die_basis_fest_danach_bleibt_sie() -> None:
    IdentifierBase.objects.all().delete()
    assert stored_identifier_base() is None
    assert not IdentifierBase.objects.exists()

    with override_settings(SITE_URL="https://alt.example/"):
        assert identifier_base() == "https://alt.example"
    with override_settings(SITE_URL="https://neu.example"):
        assert site_url() == "https://neu.example"
        assert identifier_base() == "https://alt.example"

    assert stored_identifier_base() == "https://alt.example"
    assert IdentifierBase.objects.count() == 1


@pytest.mark.django_db
def test_datenmigration_behaelt_eine_festgelegte_basis() -> None:
    """Wiederholbar: Eine schon festgelegte Basis überschreibt die Migration nicht."""
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": "https://alt.example"})
    with override_settings(SITE_URL="https://neu.example"):
        MIGRATION.basis_festlegen(django_apps, SimpleNamespace(connection=connection))
    assert stored_identifier_base() == "https://alt.example"


@pytest.mark.django_db(transaction=True)
def test_migration_uebernimmt_die_heutige_site_url() -> None:
    """Echter Lauf: Vor der Migration gibt es keine Basis, danach die damalige SITE_URL."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        assert "common_identifierbase" not in connection.introspection.table_names()
        executor = MigrationExecutor(connection)
        with override_settings(SITE_URL="https://mandari.example/"):
            executor.migrate([NACHHER])
        assert stored_identifier_base() == "https://mandari.example"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
