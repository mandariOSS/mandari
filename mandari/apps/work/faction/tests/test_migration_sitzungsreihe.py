# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenmigration der Reihen-Automatik (Issue #871): Bestand vorher = nachher.

- ``generated_until`` übernimmt je Reihe den spätesten angelegten Solltermin (gelöschte Termine kommen nicht zurück).
- Zu- und Absagen sind für den Bestand aus; nur kommende, schon eingeladene Sitzungen behalten sie.
- Teilnahmen (auch Zu- und Absagen samt Begründung und Zeitpunkt) bleiben Feld für Feld unverändert, ebenso alle
  vorhandenen Spalten der Sitzungen und Reihen; nichts wird gelöscht (Entscheidung vom 05.10.2026: nur die
  Einstellung wird umgeschaltet).
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATION_SUFFIX = "_sitzungsreihe_automatik"


def _zeilen(model: Any, spalten: list[str]) -> list[dict[str, Any]]:
    """Alle Zeilen mit den angegebenen Spalten, stabil sortiert."""
    return sorted(model.objects.values(*spalten), key=lambda zeile: str(zeile["id"]))


def _spalten(model: Any) -> list[str]:
    return [feld.attname for feld in model._meta.concrete_fields]


def _knoten() -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """Migration dieses Issues und ihre Vorgänger in ``work`` (robust gegen Umnummerierung)."""
    graph = MigrationExecutor(connection).loader.graph
    nachher = next(k for k in graph.nodes if k[0] == "work" and k[1].endswith(MIGRATION_SUFFIX))
    vorher = [p.key for p in graph.node_map[nachher].parents if p.key[0] == "work"]
    return nachher, vorher


@pytest.mark.django_db(transaction=True)
def test_migration_uebernimmt_bestand_ohne_verlust() -> None:
    nachher, vorher = _knoten()
    executor = MigrationExecutor(connection)
    executor.migrate(vorher)
    try:
        alt = executor.loader.project_state(vorher).apps
        organization_model = alt.get_model("tenants", "Organization")
        user_model = alt.get_model("accounts", "User")
        membership_model = alt.get_model("tenants", "Membership")
        schedule_model = alt.get_model("work", "FactionMeetingSchedule")
        meeting_model = alt.get_model("work", "FactionMeeting")
        attendance_model = alt.get_model("work", "FactionAttendance")

        org = organization_model.objects.create(name="Musterfraktion", slug="musterfraktion")
        user = user_model.objects.create(email="mitglied@example.org")
        membership = membership_model.objects.create(user=user, organization=org)
        reihe = schedule_model.objects.create(organization=org, name="Reihe", weekday=0, time=time(18, 0))
        leer = schedule_model.objects.create(organization=org, name="Leer", weekday=2, time=time(18, 0))
        kuenftig = timezone.now() + timedelta(days=3)
        vergangen = timezone.now() - timedelta(days=3)

        def sitzung(name: str, start: Any, **felder: Any) -> Any:
            return meeting_model.objects.create(organization=org, title=name, start=start, **felder)

        sitzung("Termin 1", kuenftig, schedule=reihe, scheduled_date=date(2026, 10, 12))
        sitzung("Termin 2", kuenftig, schedule=reihe, scheduled_date=date(2026, 10, 19))
        eingeladen = sitzung("Eingeladen", kuenftig, status="invited", invitation_sent=True)
        geplant = sitzung("Geplant", kuenftig, status="planned")
        vorbei = sitzung("Vorbei", vergangen, status="completed", invitation_sent=True)
        abgesagt = sitzung("Abgesagt", kuenftig, status="cancelled", invitation_sent=True)
        zusage = attendance_model.objects.create(
            meeting=eingeladen,
            membership=membership,
            status="confirmed",
            response_message="Komme etwas später",
            responded_at=timezone.now() - timedelta(days=1),
        )
        absage = attendance_model.objects.create(
            meeting=geplant,
            membership=membership,
            status="declined",
            response_message="Im Urlaub",
            responded_at=timezone.now() - timedelta(days=2),
        )
        attendance_model.objects.create(meeting=vorbei, membership=membership, status="present")
        attendance_model.objects.create(meeting=vorbei, is_guest=True, guest_name="Gast", status="excused")
        teilnahme_spalten = _spalten(attendance_model)
        sitzung_spalten = _spalten(meeting_model)
        reihe_spalten = _spalten(schedule_model)
        teilnahmen_vorher = _zeilen(attendance_model, teilnahme_spalten)
        sitzungen_vorher = _zeilen(meeting_model, sitzung_spalten)
        reihen_vorher = _zeilen(schedule_model, reihe_spalten)

        executor = MigrationExecutor(connection)
        executor.migrate([nachher])

        neu = MigrationExecutor(connection).loader.project_state([nachher]).apps
        schedule_neu = neu.get_model("work", "FactionMeetingSchedule")
        meeting_neu = neu.get_model("work", "FactionMeeting")
        attendance_neu = neu.get_model("work", "FactionAttendance")

        assert schedule_neu.objects.get(pk=reihe.pk).generated_until == date(2026, 10, 19)
        assert schedule_neu.objects.get(pk=leer.pk).generated_until is None
        assert set(schedule_neu.objects.values_list("rsvp_enabled", "auto_invite")) == {(False, False)}
        assert meeting_neu.objects.get(pk=eingeladen.pk).rsvp_enabled is True
        for pk in (geplant.pk, vorbei.pk, abgesagt.pk):
            assert meeting_neu.objects.get(pk=pk).rsvp_enabled is False
        # Bestand vorher = nachher: jede vorhandene Spalte jeder Zeile unverändert, keine Zeile weniger
        assert _zeilen(attendance_neu, teilnahme_spalten) == teilnahmen_vorher
        assert _zeilen(meeting_neu, sitzung_spalten) == sitzungen_vorher
        assert _zeilen(schedule_neu, reihe_spalten) == reihen_vorher
        assert attendance_neu.objects.get(pk=zusage.pk).status == "confirmed"
        assert attendance_neu.objects.get(pk=absage.pk).response_message == "Im Urlaub"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
