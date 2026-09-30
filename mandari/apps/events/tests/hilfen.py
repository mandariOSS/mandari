# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfen für Tests der Ereignistechnik.

Transaktionskennungen, Sequenz, Trigger und ``NOTIFY`` gibt es nur in PostgreSQL. Tests dafür
laufen in der CI (PostgreSQL 16) und werden lokal mit SQLite übersprungen.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.db.models import Max
from django.utils import timezone

from apps.events.models import Event, Visibility


def nur_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("braucht PostgreSQL (CI); SQLite kennt weder xid8 noch NOTIFY")


def direktverbindung(*, autocommit: bool = True) -> psycopg.Connection[Any]:
    """Eigene Verbindung zur Testdatenbank an Django vorbei, z. B. für ``LISTEN`` oder parallele Transaktionen."""
    einstellungen = connection.settings_dict
    parameter = {
        "dbname": einstellungen["NAME"],
        "user": einstellungen.get("USER"),
        "password": einstellungen.get("PASSWORD"),
        "host": einstellungen.get("HOST"),
        "port": einstellungen.get("PORT"),
    }
    return psycopg.connect(autocommit=autocommit, **{k: v for k, v in parameter.items() if v})


def ereignis_daten(**abweichend: Any) -> dict[str, Any]:
    """Pflichtfelder eines Journaleintrags mit neutralen Werten."""
    jetzt: datetime = timezone.now()
    daten: dict[str, Any] = {
        "type": "test.objekt.geaendert",
        "version": 1,
        "aggregate_type": "Objekt",
        "aggregate_id": uuid.uuid4(),
        "tenant_ref": f"org:{uuid.uuid4()}",
        "visibility": Visibility.INTERN,
        "occurred_at": jetzt,
        "correlation_id": uuid.uuid4(),
        "payload": {"changed": ["status"]},
    }
    daten.update(abweichend)
    return daten


def ereignis_anlegen(**abweichend: Any) -> Event:
    return Event.objects.create(**ereignis_daten(**abweichend))


_ROH_EINFUEGEN = """
    INSERT INTO events_event
        (event_id, type, version, aggregate_type, aggregate_id, tenant_ref, visibility, occurred_at, correlation_id,
         payload)
    VALUES (%s, 'test.objekt.geaendert', 1, 'Objekt', %s, 'org:test', 'intern', now(), %s, '{}'::jsonb)
"""


def roh_einfuegen(verbindung: psycopg.Connection[Any], aggregate_id: uuid.UUID | None = None) -> uuid.UUID:
    """Schreibt ein Ereignis über eine Direktverbindung (in deren laufender Transaktion)."""
    event_id = uuid.uuid4()
    verbindung.execute(_ROH_EINFUEGEN, (event_id, aggregate_id or uuid.uuid4(), uuid.uuid4()))
    return event_id


def nummeriert(**abweichend: Any) -> Event:
    """Legt ein Ereignis an und gibt ihm die nächste Folgenummer, wie es der Sequenzierer täte.

    Für Tests der Zustellung, auch mit SQLite; die Vergabe selbst prüfen die Tests des Sequenzierers.
    """
    ereignis = ereignis_anlegen(**abweichend)
    hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
    Event.objects.filter(pk=ereignis.pk).update(seq=hoechste + 1)
    ereignis.refresh_from_db()
    return ereignis


def folgenummern(event_ids: list[uuid.UUID]) -> list[int | None]:
    """Folgenummern der Ereignisse in der Reihenfolge der übergebenen Kennungen."""
    nach_id = dict(Event.objects.filter(event_id__in=event_ids).values_list("event_id", "seq"))
    return [nach_id.get(event_id) for event_id in event_ids]


class Sicht:
    """Behelfstabelle als Datenbank-Sicht für Tests der Zustellung.

    Je Ereignis höchstens eine Zeile (``event_id`` eindeutig): Eine doppelte Zustellung scheitert
    am Handler. ``ord`` hält die Reihenfolge der Effekte fest.
    """

    TABELLE = "events_test_sicht"

    def anlegen(self) -> None:
        schluessel = "bigserial" if connection.vendor == "postgresql" else "integer"
        zusatz = "" if connection.vendor == "postgresql" else " AUTOINCREMENT"
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.TABELLE}")
            cursor.execute(
                f"CREATE TABLE {self.TABELLE} (ord {schluessel} PRIMARY KEY{zusatz}, event_id varchar(36) NOT NULL "
                "UNIQUE, aggregate_id varchar(36) NOT NULL, seq bigint NOT NULL, schatten boolean NOT NULL)"
            )

    def entfernen(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.TABELLE}")

    def schreiben(self, events: list[Event], schatten: bool = False) -> None:
        with connection.cursor() as cursor:
            for ereignis in events:
                cursor.execute(
                    f"INSERT INTO {self.TABELLE} (event_id, aggregate_id, seq, schatten) VALUES (%s, %s, %s, %s)",
                    [str(ereignis.event_id), str(ereignis.aggregate_id), ereignis.seq, schatten],
                )

    def zeilen(self) -> list[tuple[uuid.UUID, uuid.UUID, int, bool]]:
        """(event_id, aggregate_id, seq, schatten) in der Reihenfolge der Effekte."""
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT event_id, aggregate_id, seq, schatten FROM {self.TABELLE} ORDER BY ord")
            return [(uuid.UUID(e), uuid.UUID(a), int(s), bool(sch)) for e, a, s, sch in cursor.fetchall()]

    def seqs(self) -> list[int]:
        return [seq for _, _, seq, _ in self.zeilen()]
