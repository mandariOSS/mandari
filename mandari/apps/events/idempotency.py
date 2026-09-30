# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Idempotenzspeicher (fachfrei): eine Arbeit höchstens einmal je Schlüssel ausführen.

Grundlage: ``docs/adr/20260929-befehle-synchron.md`` und der IETF-Entwurf zum Header
``Idempotency-Key``. Der Aufrufer (z. B. ``hub.commands``) nennt Bereich, Schlüssel und einen Hash
der Anfrage und übergibt die Arbeit als Funktion. ``run_once`` führt sie in einer Transaktion aus, die
zugleich den Schlüssel belegt, und speichert ihre Antwort:

- neuer Schlüssel: Arbeit ausführen, Antwort speichern, ``(Antwort, False)``;
- gleicher Schlüssel, gleicher Hash: gespeicherte Antwort, ``(Antwort, True)``, ohne erneute Arbeit;
- gleicher Schlüssel, anderer Hash: ``IdempotencyConflictError``.

Scheitert die Arbeit, rollt die Transaktion samt Schlüssel zurück; eine Wiederholung führt sie erneut
aus. Zwei gleichzeitige Anfragen mit demselben Schlüssel laufen nicht doppelt: Unter PostgreSQL wartet
die zweite am eindeutigen Index, bis die erste festgeschrieben ist, und erhält dann deren Antwort.

**Eigene Transaktion:** ``run_once`` öffnet die äußerste Transaktion selbst (``durable``). Wer eine
Antwort erhält, kann sich darauf verlassen, dass Arbeit und Schlüssel festgeschrieben sind, genau
wie bei einer Antwort über HTTP. Ein Aufruf in einer offenen Transaktion des Aufrufers ergibt
``NestedTransactionError``: Dort lägen Arbeit und Schlüssel nur in einem Sicherungspunkt, ein
späteres Rückrollen außen nähme eine schon zurückgegebene Antwort wieder weg, und die Sperre am
Schlüssel bliebe bis zum Ende der äußeren Transaktion stehen.

Antworten enthalten nur Kennungen und Codes, nie Inhalte. Der Hash der Anfrage ist ein ungesalzener
SHA-256-Wert; bei Anfragen mit wenig Entropie (wenige bekannte Kennungen, kurze Angaben) lässt sich
der Inhalt daraus erraten. Solche Hashes sind deshalb wie der Inhalt zu schützen und nach der Frist
zu löschen: ``purge`` bzw. der tägliche Zeitplan in ``schedules.py`` (``purge_expired``).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any, Final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyKey

MAX_KEY_LENGTH = 255
#: Aufbewahrung in Tagen, wenn ``EVENTS_IDEMPOTENCY_RETENTION_DAYS`` fehlt
DEFAULT_RETENTION_DAYS: Final = 30
NESTED: Final = (
    "run_once() bzw. dispatch() darf nicht in einer offenen Transaktion aufgerufen werden: "
    "Die Antwort muss festgeschrieben sein, bevor der Aufrufer sie erhält."
)


class IdempotencyConflictError(Exception):
    """Der Schlüssel ist schon für eine andere Anfrage vergeben."""


class NestedTransactionError(RuntimeError):
    """``run_once`` wurde in einer offenen Transaktion aufgerufen (Programmierfehler des Aufrufers)."""


def run_once(
    scope: str,
    key: str,
    request_hash: str,
    work: Callable[[], dict[str, Any]],
    *,
    label: str = "",
) -> tuple[dict[str, Any], bool]:
    """Führt ``work`` höchstens einmal je ``(scope, key)`` aus; liefert (Antwort, Wiederholung)."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError("Idempotenzschlüssel fehlt oder ist zu lang")
    with ExitStack() as stack:
        try:
            # durable: Beim Verlassen ist festgeschrieben (in Tests: Sicherungspunkt der Testtransaktion).
            stack.enter_context(transaction.atomic(durable=True))
        except RuntimeError:
            raise NestedTransactionError(NESTED) from None
        try:
            with transaction.atomic():
                record = IdempotencyKey.objects.create(
                    scope=scope, key=key, label=label, request_hash=request_hash, response={}
                )
        except IntegrityError:
            existing = IdempotencyKey.objects.filter(scope=scope, key=key).first()
            if existing is None:  # pragma: no cover – nur bei gleichzeitigem Aufräumen denkbar
                raise
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(label or "Idempotenzschlüssel") from None
            return dict(existing.response), True
        response = work()
        record.response = response
        record.save(update_fields=["response"])
        return response, False


def purge(before: datetime) -> int:
    """Löscht Einträge, die vor ``before`` angelegt wurden; liefert ihre Anzahl."""
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=before).delete()
    return deleted


def retention_days() -> int:
    """Aufbewahrungsfrist in Tagen aus ``EVENTS_IDEMPOTENCY_RETENTION_DAYS`` (mindestens ein Tag)."""
    days = int(getattr(settings, "EVENTS_IDEMPOTENCY_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    if days < 1:
        raise ImproperlyConfigured("EVENTS_IDEMPOTENCY_RETENTION_DAYS muss mindestens 1 sein")
    return days


def purge_expired(now: datetime | None = None) -> int:
    """Löscht Einträge, die älter als die Aufbewahrungsfrist sind; liefert ihre Anzahl."""
    return purge((now or timezone.now()) - timedelta(days=retention_days()))
