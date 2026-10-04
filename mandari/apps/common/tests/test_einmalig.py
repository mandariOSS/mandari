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


class MitEigenenArgumenten(EinmaligMixin, BaseCommand):
    """Wie die echten Cron-Commands: eigenes add_arguments ohne super()-Aufruf."""

    sperre = "eigene"
    laeufe: list[bool] = []

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--report", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        MitEigenenArgumenten.laeufe.append(bool(options["report"]))


def test_ohne_sperre_option_ueberlebt_eigenes_add_arguments() -> None:
    MitEigenenArgumenten.laeufe.clear()
    fremd = Sperre("eigene")
    fremd.inhaber = "anderer-knoten:1"
    assert fremd.erwerben()

    parser = MitEigenenArgumenten().create_parser("manage.py", "eigene")
    assert "--ohne-sperre" in parser.format_help() and "--report" in parser.format_help()

    call_command(MitEigenenArgumenten(), "--report", stderr=StringIO())
    assert MitEigenenArgumenten.laeufe == [], "gesperrt → übersprungen"
    call_command(MitEigenenArgumenten(), "--report", "--ohne-sperre")
    assert MitEigenenArgumenten.laeufe == [True]


class NurLesend(EinmaligMixin, BaseCommand):
    """Ein Befehl mit Berichtsoption wie check_source_health --report."""

    sperre = "nur-lesend"
    nur_lesend = ("report",)
    laeufe: list[str] = []

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--report", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        NurLesend.laeufe.append("bericht" if options["report"] else "probe" if options["dry_run"] else "lauf")


def test_berichte_und_probelaeufe_laufen_auch_wenn_der_zeitplan_uebernimmt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bedient ein Worker den Zeitplan, überspringt nur der ändernde Aufruf (Issue #516)."""
    import apps.common.einmalig as einmalig

    monkeypatch.setattr(einmalig, "zeitplan_uebernimmt", lambda befehl: True)
    NurLesend.laeufe.clear()

    err = StringIO()
    call_command(NurLesend(), stderr=err)
    assert NurLesend.laeufe == []
    assert "Aufruf übersprungen" in err.getvalue()

    call_command(NurLesend(), "--report")
    call_command(NurLesend(), "--dry-run")
    call_command(NurLesend(), "--trotz-zeitplan")
    assert NurLesend.laeufe == ["bericht", "probe", "lauf"]

    assert NurLesend().liest_nur({"report": True}) and NurLesend().liest_nur({"dry_run": True})
    assert not NurLesend().liest_nur({"report": False, "dry_run": False})
