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

Antworten enthalten nur Kennungen und Codes, nie Inhalte. ``purge`` löscht Einträge nach der
Aufbewahrungsfrist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction

from .models import IdempotencyKey

MAX_KEY_LENGTH = 255


class IdempotencyConflictError(Exception):
    """Der Schlüssel ist schon für eine andere Anfrage vergeben."""


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
    with transaction.atomic():
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
