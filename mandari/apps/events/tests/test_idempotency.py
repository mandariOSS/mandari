# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotenzspeicher (Issue #539): höchstens einmal je Schlüssel, Konflikt, Rückrollen, Aufräumen."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.events.idempotency import IdempotencyConflictError, purge, run_once
from apps.events.models import IdempotencyKey, Lease

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
