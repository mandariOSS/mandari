# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignistechnik der Plattform (Datendrehscheibe, Schicht 4).

Journal (transaktionale Outbox), Folgenummer, Abonnements, geparkte Ereignisse, Aufträge und
Leader-Leases in der vorhandenen PostgreSQL-Datenbank, ohne zusätzlichen Broker. Die App ist
fachfrei: Sie kennt weder Sitzungen noch Vorlagen und importiert keine Fachmodule.

Grundlagen: ``docs/adr/20260929-ereignistechnik-postgres.md`` und
``docs/adr/20260929-sequenzierer.md``.
"""

# Nur Namen ohne Modellimport: Dieses Modul lädt Django beim Start der App-Registry.
from .registry import Delivery, TargetUnavailableError, subscriber

__all__ = ["Delivery", "TargetUnavailableError", "subscriber"]
