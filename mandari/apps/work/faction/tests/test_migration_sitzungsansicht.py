# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migration der Sitzungsansicht (Issue #874): Bestand vorher = nachher.

Die Migration legt nur neue, leere Spalten an (Beginn, Sitzungsleitung, Schriftführung, Notizen je TOP). Sitzungen,
TOPs, Protokolleinträge (auch Wortbeiträge), Beschlüsse und Teilnahmen bleiben Spalte für Spalte unverändert; keine
Zeile geht verloren. Zurück auf den Stand davor bleiben die Daten ebenfalls erhalten.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATION_SUFFIX = "_sitzungsansicht"


def _zeilen(model: Any, spalten: list[str]) -> list[dict[str, Any]]:
    return sorted(model.objects.values(*spalten), key=lambda zeile: str(zeile["id"]))


def _spalten(model: Any) -> list[str]:
    return [feld.attname for feld in model._meta.concrete_fields]


def _knoten() -> tuple[tuple[str, str], list[tuple[str, str]]]:
    graph = MigrationExecutor(connection).loader.graph
    nachher = next(k for k in graph.nodes if k[0] == "work" and k[1].endswith(MIGRATION_SUFFIX))
    vorher = [p.key for p in graph.node_map[nachher].parents if p.key[0] == "work"]
    return nachher, vorher


@pytest.mark.django_db(transaction=True)
def test_migration_laesst_bestand_unveraendert() -> None:
    nachher, vorher = _knoten()
    executor = MigrationExecutor(connection)
    executor.migrate(vorher)
    try:
        alt = executor.loader.project_state(vorher).apps
        org = alt.get_model("tenants", "Organization").objects.create(name="Musterfraktion", slug="musterfraktion")
        user = alt.get_model("accounts", "User").objects.create(email="mitglied@example.org")
        membership = alt.get_model("tenants", "Membership").objects.create(user=user, organization=org)
        modelle = {
            name: alt.get_model("work", name)
            for name in (
                "FactionMeeting",
                "FactionAgendaItem",
                "FactionProtocolEntry",
                "FactionDecision",
                "FactionAttendance",
            )
        }
        sitzung = modelle["FactionMeeting"].objects.create(
            organization=org, title="Nr. 23", start=timezone.now() - timedelta(days=7), status="completed"
        )
        top = modelle["FactionAgendaItem"].objects.create(meeting=sitzung, number="2", title="Haushalt", order=1)
        for art in ("speech", "note", "decision", "action", "vote"):
            modelle["FactionProtocolEntry"].objects.create(
                meeting=sitzung,
                agenda_item=top,
                entry_type=art,
                content_encrypted=b"verschluesselt",
                speaker=membership if art == "speech" else None,
                speaker_name_snapshot="Paula Probe" if art == "speech" else "",
            )
        modelle["FactionDecision"].objects.create(agenda_item=top, result="accepted", votes_yes=5, notes="Anmerkung")
        modelle["FactionAttendance"].objects.create(
            meeting=sitzung, membership=membership, status="present", participation_type="online"
        )
        vorher_daten = {name: _zeilen(model, _spalten(model)) for name, model in modelle.items()}

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])
        neu = MigrationExecutor(connection).loader.project_state([nachher]).apps
        for name, zeilen in vorher_daten.items():
            model = neu.get_model("work", name)
            assert _zeilen(model, list(zeilen[0].keys())) == zeilen, name
        sitzung_neu = neu.get_model("work", "FactionMeeting").objects.get(pk=sitzung.pk)
        assert sitzung_neu.started_at is None and sitzung_neu.minute_taker_id is None
        assert sitzung_neu.minute_taker_name_snapshot == "" and sitzung_neu.chaired_by_name_snapshot == ""
        top_neu = neu.get_model("work", "FactionAgendaItem").objects.get(pk=top.pk)
        assert top_neu.notes_encrypted is None and top_neu.notes_updated_at is None

        # Rückweg: die Daten bleiben
        executor = MigrationExecutor(connection)
        executor.migrate(vorher)
        zurueck = MigrationExecutor(connection).loader.project_state(vorher).apps
        for name, zeilen in vorher_daten.items():
            assert _zeilen(zurueck.get_model("work", name), list(zeilen[0].keys())) == zeilen, name
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
