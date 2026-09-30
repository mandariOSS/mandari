# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotenzspeicher (Issue #539): höchstens einmal je Schlüssel, Konflikt, Rückrollen, Aufräumen."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.utils import timezone

from apps.events import schedules
from apps.events.idempotency import (
    IdempotencyConflictError,
    NestedTransactionError,
    purge,
    purge_expired,
    retention_days,
    run_once,
)
from apps.events.models import IdempotencyKey, Lease
from apps.events.schedule import Catchup, Cron, registry

pytestmark = pytest.mark.django_db

HASH = "a" * 64


def _arbeit(zaehler: list[int], antwort: dict[str, Any] | None = None) -> Any:
    def arbeit() -> dict[str, Any]:
        zaehler.append(1)
        return antwort or {"reference": f"A/{len(zaehler)}"}

    return arbeit


def test_erste_ausfuehrung_speichert_die_antwort() -> None:
    zaehler: list[int] = []
    assert run_once("org:1 -", "k", HASH, _arbeit(zaehler), label="test.befehl") == ({"reference": "A/1"}, False)
    eintrag = IdempotencyKey.objects.get()
    assert (eintrag.scope, eintrag.key, eintrag.label, eintrag.request_hash) == ("org:1 -", "k", "test.befehl", HASH)
    assert eintrag.response == {"reference": "A/1"}


def test_wiederholung_liefert_die_gespeicherte_antwort_ohne_arbeit() -> None:
    zaehler: list[int] = []
    erste = run_once("s", "k", HASH, _arbeit(zaehler))
    zweite = run_once("s", "k", HASH, _arbeit(zaehler))
    assert (erste, zweite) == (({"reference": "A/1"}, False), ({"reference": "A/1"}, True))
    assert zaehler == [1]


def test_gleicher_schluessel_andere_anfrage_ist_ein_konflikt() -> None:
    run_once("s", "k", HASH, _arbeit([]))
    with pytest.raises(IdempotencyConflictError):
        run_once("s", "k", "b" * 64, _arbeit([]))


def test_bereiche_sind_getrennt() -> None:
    zaehler: list[int] = []
    run_once("mandant-a", "k", HASH, _arbeit(zaehler))
    run_once("mandant-b", "k", "b" * 64, _arbeit(zaehler))
    assert len(zaehler) == 2


def test_scheitert_die_arbeit_rollt_alles_zurueck() -> None:
    def scheitert() -> dict[str, Any]:
        Lease.objects.create(name="nebenwirkung", holder="test", expires_at=timezone.now())
        raise RuntimeError("fachlicher Fehler")

    with pytest.raises(RuntimeError):
        run_once("s", "k", HASH, scheitert)
    assert IdempotencyKey.objects.count() == 0
    assert Lease.objects.count() == 0
    # Danach darf derselbe Schlüssel erneut arbeiten.
    assert run_once("s", "k", HASH, _arbeit([]))[1] is False


@pytest.mark.parametrize("schluessel", ["", "x" * 256])
def test_schluessel_muss_passen(schluessel: str) -> None:
    with pytest.raises(ValueError):
        run_once("s", schluessel, HASH, _arbeit([]))


def test_aufraeumen_nach_frist() -> None:
    run_once("s", "alt", HASH, _arbeit([]))
    run_once("s", "neu", HASH, _arbeit([]))
    IdempotencyKey.objects.filter(key="alt").update(created_at=timezone.now() - timedelta(days=40))
    assert purge(timezone.now() - timedelta(days=30)) == 1
    assert list(IdempotencyKey.objects.values_list("key", flat=True)) == ["neu"]


def test_befehl_zum_aufraeumen(settings: Any) -> None:
    run_once("s", "alt", HASH, _arbeit([]))
    IdempotencyKey.objects.update(created_at=timezone.now() - timedelta(days=10))
    ausgabe = StringIO()
    call_command("events_idempotency_purge", stdout=ausgabe)
    assert "0 Idempotenzschlüssel gelöscht (älter als 30 Tage)" in ausgabe.getvalue()
    settings.EVENTS_IDEMPOTENCY_RETENTION_DAYS = 7
    call_command("events_idempotency_purge", stdout=ausgabe)
    assert "1 Idempotenzschlüssel gelöscht (älter als 7 Tage)" in ausgabe.getvalue()
    with pytest.raises(CommandError):
        call_command("events_idempotency_purge", "--days", "0")


def test_frist_aus_den_einstellungen(settings: Any) -> None:
    assert retention_days() == 30
    settings.EVENTS_IDEMPOTENCY_RETENTION_DAYS = 7
    assert retention_days() == 7
    settings.EVENTS_IDEMPOTENCY_RETENTION_DAYS = 0
    with pytest.raises(ImproperlyConfigured):
        retention_days()
    with pytest.raises(CommandError):
        call_command("events_idempotency_purge")


def test_aufraeumen_nach_der_frist_der_einstellungen(settings: Any) -> None:
    run_once("s", "alt", HASH, _arbeit([]))
    run_once("s", "neu", HASH, _arbeit([]))
    IdempotencyKey.objects.filter(key="alt").update(created_at=timezone.now() - timedelta(days=31))
    assert purge_expired() == 1
    settings.EVENTS_IDEMPOTENCY_RETENTION_DAYS = 400
    assert purge_expired(timezone.now() + timedelta(days=399)) == 0
    assert purge_expired(timezone.now() + timedelta(days=401)) == 1


def test_zeitplan_raeumt_taeglich_auf() -> None:
    """Ohne Zeitplan blieben Schlüssel und Hashes unbegrenzt liegen."""
    eintrag = registry.get("apps.events.schedules.idempotenzschluessel_aufraeumen")
    assert eintrag is not None
    assert eintrag.task is schedules.idempotenzschluessel_aufraeumen
    assert eintrag.trigger == Cron("40 3 * * *")
    assert eintrag.catchup == Catchup.NACHHOLEN
    run_once("s", "alt", HASH, _arbeit([]))
    IdempotencyKey.objects.update(created_at=timezone.now() - timedelta(days=31))
    assert schedules.idempotenzschluessel_aufraeumen.call() == 1
    assert IdempotencyKey.objects.count() == 0


# --- Eigene Transaktion -----------------------------------------------------------------------


def test_in_offener_transaktion_des_aufrufers_abgelehnt() -> None:
    zaehler: list[int] = []
    with transaction.atomic(), pytest.raises(NestedTransactionError):
        run_once("s", "k", HASH, _arbeit(zaehler))
    assert zaehler == []
    assert IdempotencyKey.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_antwort_ist_festgeschrieben() -> None:
    gesehen: list[bool] = []

    def arbeit() -> dict[str, Any]:
        gesehen.append(connection.in_atomic_block)
        return {"reference": "A/1"}

    assert run_once("s", "k", HASH, arbeit) == ({"reference": "A/1"}, False)
    assert gesehen == [True]
    assert not connection.in_atomic_block
    transaction.set_autocommit(False)
    try:
        transaction.rollback()
    finally:
        transaction.set_autocommit(True)
    assert IdempotencyKey.objects.count() == 1
