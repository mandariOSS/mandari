# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Transaktionskennung der schreibenden Transaktion (``xid8``).

Jede Journalzeile trägt die Kennung der Transaktion, die sie geschrieben hat. Die Datenbank setzt
sie selbst über den Spaltenstandard ``pg_current_xact_id()``; bei Sicherungspunkten ist das die
Kennung der Haupttransaktion. Der Sequenzierer vergibt Folgenummern nur an Zeilen, deren
Transaktion älter ist als die älteste noch laufende (``docs/adr/20260929-sequenzierer.md``).

``xid8`` ist 64 Bit breit und läuft nicht über; es gibt den Typ ab PostgreSQL 13. Andere
Datenbanken (SQLite in lokalen Tests) bekommen eine gewöhnliche Ganzzahlspalte mit Standard 0,
dort gibt es keinen Sequenzierer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models.expressions import Expression
from django.db.models.sql.compiler import SQLCompiler

if TYPE_CHECKING:
    _BigIntegerField = models.BigIntegerField[int, int]
else:
    _BigIntegerField = models.BigIntegerField


class TransactionIdField(_BigIntegerField):
    """Spalte vom Typ ``xid8``; gelesen als ``int``, geschrieben nur von der Datenbank."""

    description = "Transaktionskennung (xid8)"

    def db_type(self, connection: BaseDatabaseWrapper) -> str:
        if connection.vendor == "postgresql":
            return "xid8"
        return str(super().db_type(connection))

    def from_db_value(self, value: Any, expression: Any, connection: BaseDatabaseWrapper) -> int | None:
        # psycopg kennt keinen Lader für xid8 und liefert den Text der Zahl.
        return None if value is None else int(value)

    def get_db_prep_value(self, value: Any, connection: BaseDatabaseWrapper, prepared: bool = False) -> Any:
        # Von bigint gibt es keinen Cast nach xid8; ein Textwert wird angenommen. Nötig, wenn ein
        # gespeichertes Ereignis erneut gespeichert oder nach ``xid`` gefiltert wird.
        value = super().get_db_prep_value(value, connection, prepared)
        if value is not None and connection.vendor == "postgresql":
            return str(value)
        return value


class CurrentTransactionId(Expression):
    """``pg_current_xact_id()`` als Spaltenstandard; außerhalb von PostgreSQL die Konstante 0."""

    output_field = TransactionIdField()
    # Als Spaltenstandard zulässig (sonst fields.E012)
    allowed_default = True

    def as_sql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, tuple[str | int, ...]]:
        if connection.vendor == "postgresql":
            return "pg_current_xact_id()", ()
        return "0", ()
