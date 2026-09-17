# SPDX-License-Identifier: AGPL-3.0-or-later
"""Singleton-Sperre für zeitgesteuerte Jobs (#55): nur ein Lauf, Verfall, Freigabe, Command-Mixin."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.common.einmalig import EinmaligMixin, Sperre


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def test_zweiter_erwerb_scheitert_bis_zur_freigabe() -> None:
    erste, zweite = Sperre("job", ttl=60), Sperre("job", ttl=60)
    zweite.inhaber = "anderer-knoten:1"
    assert erste.erwerben() is True
    assert zweite.erwerben() is False
    assert zweite.andere_instanz() == erste.inhaber
    erste.freigeben()
    assert zweite.erwerben() is True


def test_freigabe_loescht_fremde_sperre_nicht() -> None:
    erste, zweite = Sperre("job", ttl=60), Sperre("job", ttl=60)
    zweite.inhaber = "anderer-knoten:1"
    assert erste.erwerben()
    zweite.freigeben()  # nie erworben → darf die Sperre der ersten nicht anrühren
    assert cache.get(erste.schluessel) == erste.inhaber
    assert zweite.verlaengern() is False
    assert erste.verlaengern() is True


def test_kontextmanager_gibt_frei() -> None:
    with Sperre("job") as erhalten:
        assert erhalten
        assert Sperre("job").erwerben() is False
    assert Sperre("job").erwerben() is True


class Zaehler(EinmaligMixin, BaseCommand):
    sperre = "zaehler"
    laeufe = 0

    def handle(self, *args: Any, **options: Any) -> None:
        Zaehler.laeufe += 1
        assert cache.get("einmalig:zaehler"), "Sperre muss während des Laufs gehalten werden"


def test_command_mixin_ueberspringt_bei_gehaltener_sperre() -> None:
    Zaehler.laeufe = 0
    fremd = Sperre("zaehler")
    fremd.inhaber = "anderer-knoten:1"
    assert fremd.erwerben()

    err = StringIO()
    call_command(Zaehler(), stderr=err)
    assert Zaehler.laeufe == 0
    assert "läuft bereits auf anderer-knoten:1" in err.getvalue()

    call_command(Zaehler(), ohne_sperre=True)
    assert Zaehler.laeufe == 1

    fremd.freigeben()
    call_command(Zaehler())
    assert Zaehler.laeufe == 2
    assert cache.get("einmalig:zaehler") is None, "nach dem Lauf freigegeben"
