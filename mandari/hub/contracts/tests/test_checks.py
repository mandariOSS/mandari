# SPDX-License-Identifier: AGPL-3.0-or-later
"""Systemprüfung des Vertragsregisters (``manage.py check``, Issue #517)."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.apps import apps
from django.core import checks

from hub.contracts import checks as vertrags_checks
from hub.contracts.tests.hilfen import ablegen, ereignis_schema


def test_app_ist_installiert_und_hat_keine_modelle() -> None:
    konfiguration = apps.get_app_config("hub_contracts")
    assert konfiguration.name == "hub.contracts"
    assert list(konfiguration.get_models()) == []


def test_ausgeliefertes_register_besteht_die_pruefung() -> None:
    assert vertrags_checks.check_contracts() == []


def test_pruefung_ist_registriert() -> None:
    assert vertrags_checks.check_contracts in checks.registry.registry.get_checks()


def test_fehlerhafte_schemas_werden_gemeldet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ablegen(tmp_path, "ris.paper.released", 1, ereignis_schema(**{"x-visibility": "geheim"}))
    ablegen(tmp_path, "ris.paper.release", 1, ereignis_schema("ris.paper.release"))
    monkeypatch.setattr(vertrags_checks, "SCHEMA_ROOT", tmp_path)

    meldungen = vertrags_checks.check_contracts()

    assert {meldung.id for meldung in meldungen} == {"hub_contracts.E002"}
    assert all(meldung.level == checks.ERROR for meldung in meldungen)
    texte = [meldung.msg for meldung in meldungen]
    assert "ris.paper.released v1: x-visibility: unbekannte Klasse „geheim“" in texte
    assert any(text.startswith("ris.paper.release v1: Ereignisse stehen in der Vergangenheitsform") for text in texte)


def test_fehlerhafte_huelle_wird_gemeldet(monkeypatch: pytest.MonkeyPatch) -> None:
    kaputt = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "wolke"}
    monkeypatch.setattr(vertrags_checks, "envelope_schema", lambda: kaputt)

    meldungen = vertrags_checks.check_contracts()

    assert [meldung.id for meldung in meldungen] == ["hub_contracts.E001"]
    assert meldungen[0].msg.startswith("Ereignishülle: kein gültiges JSON Schema 2020-12")
