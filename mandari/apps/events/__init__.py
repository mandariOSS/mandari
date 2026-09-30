# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignistechnik der Plattform (Datendrehscheibe, Schicht 4).

Journal (transaktionale Outbox), Folgenummer, Abonnements, geparkte Ereignisse, Aufträge und
Leader-Leases in der vorhandenen PostgreSQL-Datenbank, ohne zusätzlichen Broker. Die App ist
fachfrei: Sie kennt weder Sitzungen noch Vorlagen und importiert keine Fachmodule.

Fachmodule schreiben Ereignisse ausschließlich mit ``publish()`` (``apps.events.publishing``) und
empfangen sie mit ``@subscriber`` (``apps.events.registry``).

Grundlagen: ``docs/adr/20260929-ereignistechnik-postgres.md`` und
``docs/adr/20260929-sequenzierer.md``.
"""

# Nur Namen ohne Modellimport: Dieses Modul lädt Django beim Start der App-Registry.
from .publishing import (
    CanonicalRef,
    InvalidEventError,
    PublishOutsideTransactionError,
    event_context,
    publish,
    system_ref,
    tenant_ref,
    user_ref,
)
from .registry import Delivery, TargetUnavailableError, subscriber

__all__ = [
    "CanonicalRef",
    "Delivery",
    "InvalidEventError",
    "PublishOutsideTransactionError",
    "TargetUnavailableError",
    "event_context",
    "publish",
    "subscriber",
    "system_ref",
    "tenant_ref",
    "user_ref",
]
