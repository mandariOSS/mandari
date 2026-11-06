# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration der Sitzungsarten (Issue #1231): Bestand vorher = nachher.

- Jede Organisation bekommt genau eine Standardart „Fraktionssitzung“ (Teilnehmende alle, Sichtbarkeit Mitglieder,
  Standard-Tagesordnung, nichtöffentlicher Teil und Abstimmungen an, Startnummer 1), auch ohne Sitzungen.
- Alle Reihen, Sitzungen und Standard-TOPs gehören danach zur Standardart ihrer Organisation.
- Alle übrigen Spalten bleiben Zeile für Zeile unverändert (auch Nummern und ``updated_at``), ebenso
  ``organization.settings``; nichts wird gelöscht. Die Fraktionssitzungen zählen weiter.
- Rückwärts verschwinden Tabelle und Spalten, der Bestand bleibt unverändert; ein zweiter Lauf legt nichts doppelt an.
"""

from __future__ import annotations

import importlib
from datetime import date, time, timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

SCHEMA_SUFFIX = "_sitzungsarten"
DATEN_SUFFIX = "_sitzungsarten_zuordnen"


def _zeilen(model: Any, spalten: list[str]) -> list[dict[str, Any]]:
    """Alle Zeilen mit den angegebenen Spalten, stabil sortiert."""
    return sorted(model.objects.values(*spalten), key=lambda zeile: str(zeile["id"]))


def _spalten(model: Any) -> list[str]:
    return [feld.attname for feld in model._meta.concrete_fields]


def _knoten() -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """Datenmigration dieses Issues und die Vorgänger der Schemamigration in ``work`` (robust gegen Umnummerierung)."""
    graph = MigrationExecutor(connection).loader.graph
    schema = next(k for k in graph.nodes if k[0] == "work" and k[1].endswith(SCHEMA_SUFFIX))
    nachher = next(k for k in graph.nodes if k[0] == "work" and k[1].endswith(DATEN_SUFFIX))
    vorher = [p.key for p in graph.node_map[schema].parents if p.key[0] == "work"]
    return nachher, vorher


@pytest.mark.django_db(transaction=True)
def test_migration_ordnet_bestand_der_standardart_zu_ohne_verlust() -> None:
    nachher, vorher = _knoten()
    executor = MigrationExecutor(connection)
    executor.migrate(vorher)
    try:
        alt = executor.loader.project_state(vorher).apps
        organization_model = alt.get_model("tenants", "Organization")
        schedule_model = alt.get_model("work", "FactionMeetingSchedule")
        meeting_model = alt.get_model("work", "FactionMeeting")
        standard_model = alt.get_model("work", "FactionStandardAgendaItem")

        einstellungen = {"faction": {"link_previous_meeting": True, "first_agenda_title_no_previous": "TO {nr}"}}
        org = organization_model.objects.create(name="Musterfraktion", slug="musterfraktion", settings=einstellungen)
        andere = organization_model.objects.create(name="Andere Fraktion", slug="andere-fraktion")
        leer = organization_model.objects.create(name="Leere Fraktion", slug="leere-fraktion")
        reihe = schedule_model.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))
        schedule_model.objects.create(organization=andere, name="Andere Reihe", weekday=2, time=time(19, 0))
        standard_model.objects.create(organization=org, title="Beschlüsse", order=1)
        standard_model.objects.create(organization=andere, title="Termine", visibility="internal", order=1)
        vergangen = timezone.now() - timedelta(days=14)
        kuenftig = timezone.now() + timedelta(days=3)
        erste = meeting_model.objects.create(
            organization=org, title="Erste", start=vergangen, status="completed", meeting_number=1
        )
        meeting_model.objects.create(
            organization=org,
            title="Zweite",
            start=kuenftig,
            status="planned",
            meeting_number=2,
            previous_meeting=erste,
            schedule=reihe,
            scheduled_date=date(2026, 10, 12),
        )
        meeting_model.objects.create(organization=org, title="Entfällt", start=kuenftig, status="cancelled")
        meeting_model.objects.create(
            organization=andere, title="Andere", start=vergangen, status="completed", meeting_number=17
        )
        spalten = {model: _spalten(model) for model in (schedule_model, meeting_model, standard_model)}
        bestand_vorher = {model: _zeilen(model, felder) for model, felder in spalten.items()}
        settings_vorher = dict(organization_model.objects.values_list("pk", "settings"))

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])

        neu = MigrationExecutor(connection).loader.project_state([nachher]).apps
        meeting_type_neu = neu.get_model("work", "FactionMeetingType")
        organization_neu = neu.get_model("tenants", "Organization")

        # Genau eine Standardart je Organisation, auch ohne Sitzungen
        arten = list(meeting_type_neu.objects.all())
        assert sorted(art.organization_id for art in arten) == sorted([org.pk, andere.pk, leer.pk])
        for art in arten:
            assert art.name == "Fraktionssitzung"
            assert art.is_default and art.is_active
            assert (art.position, art.number_start) == (0, 1)
            assert (art.participants_mode, art.visibility, art.agenda_mode) == ("all", "members", "standard")
            assert art.non_public_enabled and art.voting_enabled
            assert art.settings == {}
            assert art.participants_role_id is None and art.participants_committee_id is None
        standardart = {art.organization_id: art.pk for art in arten}

        # Jede Reihe, Sitzung und jeder Standard-TOP gehört zur Standardart der eigenen Organisation
        for name in ("FactionMeetingSchedule", "FactionMeeting", "FactionStandardAgendaItem"):
            for organization_id, meeting_type_id in neu.get_model("work", name).objects.values_list(
                "organization_id", "meeting_type_id"
            ):
                assert meeting_type_id == standardart[organization_id], name

        # Bestand vorher = nachher: jede vorhandene Spalte jeder Zeile unverändert, keine Zeile weniger
        for model, felder in spalten.items():
            assert _zeilen(neu.get_model("work", model.__name__), felder) == bestand_vorher[model], model.__name__
        assert dict(organization_neu.objects.values_list("pk", "settings")) == settings_vorher

        # Die Fraktionssitzungen zählen weiter
        from apps.work.faction.models import FactionMeeting, FactionMeetingType

        art_org = FactionMeetingType.objects.get(pk=standardart[org.pk])
        assert FactionMeeting.get_next_meeting_number(org.pk, art_org) == 3
        assert FactionMeeting.get_next_meeting_number(andere.pk) == 18
        assert FactionMeeting.get_next_meeting_number(leer.pk) == 1

        # Ein zweiter Lauf legt nichts doppelt an und ändert nichts
        modul = importlib.import_module(f"apps.work.migrations.{nachher[1]}")
        with connection.schema_editor() as editor:
            modul.zuordnen(neu, editor)
        assert meeting_type_neu.objects.count() == 3

        # Rückweg: Tabelle und Spalten weg, Bestand unverändert
        executor = MigrationExecutor(connection)
        executor.migrate(vorher)
        zurueck = MigrationExecutor(connection).loader.project_state(vorher).apps
        for model, felder in spalten.items():
            assert _zeilen(zurueck.get_model("work", model.__name__), felder) == bestand_vorher[model], model.__name__
        with connection.cursor() as cursor:
            assert "work_factionmeetingtype" not in connection.introspection.table_names(cursor)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
