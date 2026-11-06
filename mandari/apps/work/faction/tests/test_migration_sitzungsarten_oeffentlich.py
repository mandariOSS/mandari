# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration „Standardart öffentlich“ (Issue #1239).

- Die Standardart wird öffentlich, wenn die öffentliche API oder die öffentlichen Protokolle der Organisation an sind;
  sonst bleibt sie „alle Mitglieder“. „Nur Teilnehmende“ und weitere Arten bleiben unverändert.
- Sonst ändert sich nichts: alle übrigen Spalten der Sitzungsarten und alle Sitzungen bleiben Zeile für Zeile gleich.
- Rückwärts sind alle Standardarten wieder „alle Mitglieder“ (Stand nach der Zuordnung); ein zweiter Lauf ändert nichts.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

SUFFIX = "_sitzungsarten_oeffentlich"


def _knoten() -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """Diese Datenmigration und ihre Vorgänger in ``work`` (robust gegen Umnummerierung)."""
    graph = MigrationExecutor(connection).loader.graph
    nachher = next(k for k in graph.nodes if k[0] == "work" and k[1].endswith(SUFFIX))
    vorher = [p.key for p in graph.node_map[nachher].parents if p.key[0] == "work"]
    return nachher, vorher


def _zeilen(model: Any, ohne: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    spalten = [f.attname for f in model._meta.concrete_fields if f.attname not in ohne]
    return sorted(model.objects.values(*spalten), key=lambda zeile: str(zeile["id"]))


@pytest.mark.django_db(transaction=True)
def test_standardart_wird_oeffentlich_wenn_api_oder_protokolle_an() -> None:
    nachher, vorher = _knoten()
    executor = MigrationExecutor(connection)
    executor.migrate(vorher)
    try:
        alt = executor.loader.project_state(vorher).apps
        organization_model = alt.get_model("tenants", "Organization")
        meeting_type_model = alt.get_model("work", "FactionMeetingType")
        access_model = alt.get_model("work", "FactionPublicApiAccess")
        meeting_model = alt.get_model("work", "FactionMeeting")

        def org_mit_art(slug: str, **felder: Any) -> Any:
            org = organization_model.objects.create(name=slug, slug=slug, **felder)
            meeting_type_model.objects.create(organization=org, name="Fraktionssitzung", is_default=True)
            return org

        api = org_mit_art("api")
        access_model.objects.create(organization=api, is_enabled=True, token="t-api")
        protokolle = org_mit_art("protokolle", publish_protocols=True)
        api_aus = org_mit_art("api-aus")
        access_model.objects.create(organization=api_aus, is_enabled=False, token="t-aus")
        nichts = org_mit_art("nichts")
        vorstand = org_mit_art("vorstand", publish_protocols=True)
        meeting_type_model.objects.filter(organization=vorstand).update(visibility="participants")
        weitere = meeting_type_model.objects.create(organization=api, name="AG", visibility="members")
        meeting_model.objects.create(
            organization=api,
            meeting_type=meeting_type_model.objects.get(organization=api, is_default=True),
            title="Sitzung",
            start=timezone.now() + timedelta(days=1),
        )
        arten_vorher = _zeilen(meeting_type_model, ohne=("visibility",))
        sitzungen_vorher = _zeilen(meeting_model)

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])
        neu = MigrationExecutor(connection).loader.project_state([nachher]).apps
        arten = neu.get_model("work", "FactionMeetingType")

        def sichtbarkeit(org: Any) -> str:
            return str(arten.objects.get(organization_id=org.pk, is_default=True).visibility)

        assert sichtbarkeit(api) == "public"
        assert sichtbarkeit(protokolle) == "public"
        assert sichtbarkeit(api_aus) == "members"
        assert sichtbarkeit(nichts) == "members"
        assert sichtbarkeit(vorstand) == "participants"
        assert arten.objects.get(pk=weitere.pk).visibility == "members"
        # Sonst unverändert
        assert _zeilen(arten, ohne=("visibility",)) == arten_vorher
        assert _zeilen(neu.get_model("work", "FactionMeeting")) == sitzungen_vorher

        # Ein zweiter Lauf ändert nichts
        modul = importlib.import_module(f"apps.work.migrations.{nachher[1]}")
        with connection.schema_editor() as editor:
            modul.oeffentlich_machen(neu, editor)
        assert (sichtbarkeit(api), sichtbarkeit(nichts), sichtbarkeit(vorstand)) == (
            "public",
            "members",
            "participants",
        )

        # Rückweg: Standardarten wie nach der Zuordnung, „nur Teilnehmende“ bleibt
        executor = MigrationExecutor(connection)
        executor.migrate(vorher)
        zurueck = (
            MigrationExecutor(connection).loader.project_state(vorher).apps.get_model("work", "FactionMeetingType")
        )
        assert set(zurueck.objects.filter(is_default=True).values_list("visibility", flat=True)) == {
            "members",
            "participants",
        }
        assert _zeilen(zurueck, ohne=("visibility",)) == arten_vorher
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
