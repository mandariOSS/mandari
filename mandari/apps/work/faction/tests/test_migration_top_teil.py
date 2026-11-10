# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration „Teil der Tagesordnung nur aus festen Werten“.

- TOPs mit einem anderen Wert als „öffentlich“ oder „nichtöffentlich“ werden nichtöffentlich.
- Sonst ändert sich nichts: gültige TOPs und alle übrigen Spalten bleiben Zeile für Zeile gleich.
- Ein zweiter Lauf ändert nichts; danach lässt sich jeder TOP wieder speichern.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

SUFFIX = "_top_teil_feste_werte"


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
def test_unbekannter_teil_wird_nichtoeffentlich() -> None:
    nachher, vorher = _knoten()
    executor = MigrationExecutor(connection)
    executor.migrate(vorher)
    try:
        alt = executor.loader.project_state(vorher).apps
        org = alt.get_model("tenants", "Organization").objects.create(name="Fraktion", slug="fraktion")
        art = alt.get_model("work", "FactionMeetingType").objects.create(
            organization=org, name="Fraktionssitzung", is_default=True
        )
        sitzung = alt.get_model("work", "FactionMeeting").objects.create(
            organization=org, meeting_type=art, title="Sitzung", start=timezone.now() + timedelta(days=1)
        )
        item_model = alt.get_model("work", "FactionAgendaItem")
        oeffentlich = item_model.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public")
        intern = item_model.objects.create(meeting=sitzung, number="N1", title="Personal", visibility="internal")
        fremd = item_model.objects.create(meeting=sitzung, number="2", title="Präpariert", visibility="'});x();({a:'")
        leer = item_model.objects.create(meeting=sitzung, number="3", title="Leer", visibility="")
        vorher_zeilen = _zeilen(item_model, ohne=("visibility",))

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])
        neu = MigrationExecutor(connection).loader.project_state([nachher]).apps.get_model("work", "FactionAgendaItem")

        def teil(item: Any) -> str:
            return str(neu.objects.get(pk=item.pk).visibility)

        assert (teil(oeffentlich), teil(intern), teil(fremd), teil(leer)) == (
            "public",
            "internal",
            "internal",
            "internal",
        )
        assert _zeilen(neu, ohne=("visibility",)) == vorher_zeilen

        # Ein zweiter Lauf ändert nichts
        modul = importlib.import_module(f"apps.work.migrations.{nachher[1]}")
        with connection.schema_editor() as editor:
            modul.bereinigen(MigrationExecutor(connection).loader.project_state([nachher]).apps, editor)
        assert (teil(oeffentlich), teil(fremd)) == ("public", "internal")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    # Mit dem heutigen Modell lässt sich der bereinigte TOP wieder speichern
    from apps.work.faction.models import FactionAgendaItem

    bereinigt = FactionAgendaItem.objects.get(pk=fremd.pk)
    bereinigt.title = "Bereinigt"
    bereinigt.save()
    assert FactionAgendaItem.objects.get(pk=fremd.pk).title == "Bereinigt"
