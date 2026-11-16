# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``loeschabgleich``: Die Schritte des Laufs hängen nicht aneinander. Scheitert einer unerwartet, etwa an einer
Adresse aus einer Quelle, laufen die übrigen trotzdem, und der Lauf meldet den Fehlschlag am Ende.
"""

from __future__ import annotations

from collections import Counter
from io import StringIO
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from insight_core.services import file_reconcile, file_store

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def _schritte(monkeypatch: pytest.MonkeyPatch, scheitert: str) -> list[str]:
    """Ersetzt die Schritte des Abgleichs; der Schritt ``scheitert`` wirft, die anderen merken sich ihren Lauf."""
    gelaufen: list[str] = []

    def schritt(name: str, ergebnis: Any) -> Any:
        def lauf(*args: Any, **kwargs: Any) -> Any:
            gelaufen.append(name)
            if name == scheitert:
                raise ValueError("Invalid IPv6 URL")
            return ergebnis

        return lauf

    monkeypatch.setattr(file_reconcile, "check_changed", schritt("geaendert", Counter({"unchanged": 2})))
    monkeypatch.setattr(file_reconcile, "sample_heads", schritt("stichproben", Counter({"present": 3})))
    monkeypatch.setattr(file_reconcile, "restore_reappeared", schritt("freigeben", 1))
    monkeypatch.setattr(file_reconcile, "purge_expired", schritt("loeschen", Counter({"purged": 4})))
    monkeypatch.setattr(file_store, "uses_blobs", lambda: True)
    monkeypatch.setattr(file_store, "cleanup_orphans", schritt("verwaist", Counter({"removed": 5})))
    return gelaufen


@pytest.mark.parametrize("scheitert", ["geaendert", "stichproben", "freigeben", "loeschen"])
def test_gescheiterter_schritt_haelt_die_uebrigen_nicht_auf(monkeypatch: pytest.MonkeyPatch, scheitert: str) -> None:
    gelaufen = _schritte(monkeypatch, scheitert)
    out = StringIO()

    with pytest.raises(CommandError, match="unerwartet gescheitert.*ValueError"):
        call_command("loeschabgleich", "--interval", "0", stdout=out)

    assert gelaufen == ["geaendert", "stichproben", "freigeben", "loeschen", "verwaist"]
    ausgabe = out.getvalue()
    erwartet = {
        "geaendert": "Geänderte Dokumente: unchanged=2",
        "stichproben": "Stichproben: present=3",
        "freigeben": "Wieder freigegeben (Text wird neu erkannt): 1",
        "loeschen": "purged=4",
    }
    for name, zeile in erwartet.items():
        assert (zeile in ausgabe) == (name != scheitert), (name, ausgabe)
    assert "Verwaiste Inhalte: removed=5" in ausgabe


def test_ohne_fehler_endet_der_lauf_wie_bisher(monkeypatch: pytest.MonkeyPatch) -> None:
    gelaufen = _schritte(monkeypatch, "keiner")
    out = StringIO()

    call_command("loeschabgleich", "--interval", "0", stdout=out)

    assert gelaufen == ["geaendert", "stichproben", "freigeben", "loeschen", "verwaist"]
    assert "Geänderte Dokumente: unchanged=2" in out.getvalue() and "purged=4" in out.getvalue()


def test_abgebrochener_abgleich_verwirft_seine_vormerkungen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bricht ein Abgleich mitten im Lauf ab, sperrt der nächste Schritt nicht dessen ungeprüfte Vormerkungen."""
    vorgemerkt: list[int] = []

    def geaendert(*args: Any, run: Any, **kwargs: Any) -> Any:
        run.defer_missing(object(), timezone.now())
        raise ValueError("abgebrochen")

    def stichproben(*args: Any, run: Any, **kwargs: Any) -> Any:
        vorgemerkt.append(sum(len(eintraege) for eintraege in run._pending.values()))
        return Counter({"present": 1})

    _schritte(monkeypatch, "keiner")
    monkeypatch.setattr(file_reconcile, "check_changed", geaendert)
    monkeypatch.setattr(file_reconcile, "sample_heads", stichproben)
    monkeypatch.setattr(file_reconcile, "_source_key", lambda file_obj: "quelle")

    with pytest.raises(CommandError, match="Geänderte Dokumente"):
        call_command("loeschabgleich", "--interval", "0", stdout=StringIO())

    assert vorgemerkt == [0]
