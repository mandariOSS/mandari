# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compose-Rollenprofile (#55): das Prüfskript läuft im Testlauf mit, damit die CI es ohne Docker abdeckt."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_compose_roles.py"


def _lade_skript() -> Any:
    spec = importlib.util.spec_from_file_location("check_compose_roles", SKRIPT)
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules["check_compose_roles"] = modul
    spec.loader.exec_module(modul)
    return modul


def test_rollen_decken_alle_dienste_genau_einmal() -> None:
    modul = _lade_skript()
    assert modul.pruefe() == []


def test_basisdatei_bleibt_profilfrei() -> None:
    modul = _lade_skript()
    basis = modul._lade(modul.BASIS)["services"]
    assert basis and all("profiles" not in d for d in basis.values())
    assert {
        "postgres",
        "redis",
        "elasticsearch",
        "mandari",
        "caddy",
        "website",
        "ingestor",
        "minutes-orchestrator",
    } <= set(basis)
