# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Leader-Rollen über die Tabelle ``events_lease`` (Sequenzierer, später Zeitpläne).

Sitzungsgebundene Advisory-Locks sind verboten, weil ein Verbindungspooler im Transaktionsmodus
sie nicht trägt. Stattdessen hält ein Prozess eine Zeile mit Ablaufzeit und erneuert sie
regelmäßig; läuft sie ab, darf ein anderer übernehmen.

Eine Lease sorgt nur dafür, dass normalerweise genau ein Prozess arbeitet. Wo zwei gleichzeitige
Leader Schaden anrichten würden, prüft die Arbeit selbst in ihrer Transaktion mit ``fence()``, ob
die Lease noch ihr gehört, und sperrt die Zeile bis zum Commit. Eine Übernahme wartet dann, bis
diese Transaktion abgeschlossen ist.

Ablaufzeiten rechnet und vergleicht die Datenbank (``Now()``), nicht die Uhr des Prozesses: Worker
auf mehreren Rechnern mit Uhrenversatz reichen die Lease sonst hin und her.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import timedelta

from django.db import IntegrityError, models, transaction
from django.db.models.functions import Now

from .models import Lease

#: Ablauf einer Lease ohne Erneuerung
LEASE_TTL = timedelta(seconds=30)
#: So oft erneuert der Inhaber
RENEW_INTERVAL = timedelta(seconds=10)


class LeaseLostError(Exception):
    """Die Lease gehört nicht (mehr) diesem Prozess."""


def new_holder_id() -> str:
    """Kennung eines Prozesses: Rechnername, Prozessnummer und ein Zufallsanteil."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _ablauf(ttl: timedelta) -> models.Expression:
    return models.ExpressionWrapper(Now() + ttl, output_field=models.DateTimeField())


def acquire(name: str, holder: str, ttl: timedelta = LEASE_TTL) -> bool:
    """Übernimmt oder verlängert die Lease ``name``; ``True``, wenn ``holder`` sie danach hält.

    Übernommen wird nur eine abgelaufene Lease oder die eigene, beides nach Datenbankzeit. Hält
    gerade ein ``fence()`` des bisherigen Inhabers die Zeile, wartet die Übernahme bis zu dessen
    Commit und prüft dann erneut; die Arbeit des bisherigen Inhabers ist dann schon abgeschlossen.
    """
    with transaction.atomic():
        uebernommen = (
            Lease.objects.filter(name=name)
            .filter(models.Q(holder=holder) | models.Q(expires_at__lte=Now()))
            .update(holder=holder, expires_at=_ablauf(ttl))
        )
        if uebernommen:
            return True
        if Lease.objects.filter(name=name).exists():
            return False  # gültige Lease eines anderen Prozesses
        try:
            with transaction.atomic():
                Lease.objects.create(name=name, holder=holder, expires_at=_ablauf(ttl))
        except IntegrityError:
            return False  # gleichzeitig von einem anderen Prozess angelegt
        return True


def release(name: str, holder: str) -> None:
    """Gibt die eigene Lease frei, damit ein anderer Prozess sofort übernehmen kann."""
    Lease.objects.filter(name=name, holder=holder).delete()


def fence(name: str, holder: str) -> None:
    """Prüft in der laufenden Transaktion, dass ``holder`` die Lease hält, und sperrt sie bis zum Commit.

    Die Ablaufzeit spielt hier keine Rolle: Solange niemand übernommen hat, ist die Arbeit sicher.
    Eine Übernahme ändert die Zeile und wartet deshalb auf diese Transaktion.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("fence() braucht eine offene Transaktion (transaction.atomic)")
    gesperrt = Lease.objects.select_for_update().filter(name=name, holder=holder).values_list("name", flat=True)
    if not list(gesperrt):
        raise LeaseLostError(name)
