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
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

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


def acquire(name: str, holder: str, ttl: timedelta = LEASE_TTL) -> bool:
    """Übernimmt oder verlängert die Lease ``name``; ``True``, wenn ``holder`` sie danach hält.

    Übernommen wird nur eine abgelaufene Lease oder die eigene. Die Zeile bleibt dabei bis zum
    Commit gesperrt, sodass ein ``fence()`` eines anderen Prozesses entweder vorher abgeschlossen
    ist oder danach die neue Inhaberschaft sieht.
    """
    jetzt = timezone.now()
    with transaction.atomic():
        lease = Lease.objects.select_for_update().filter(name=name).first()
        if lease is None:
            try:
                with transaction.atomic():
                    Lease.objects.create(name=name, holder=holder, expires_at=jetzt + ttl)
            except IntegrityError:
                return False  # gleichzeitig von einem anderen Prozess angelegt
            return True
        if lease.holder != holder and lease.expires_at > jetzt:
            return False
        lease.holder = holder
        lease.expires_at = jetzt + ttl
        lease.save(update_fields=["holder", "expires_at"])
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
