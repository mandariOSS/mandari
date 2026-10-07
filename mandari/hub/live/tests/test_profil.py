# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einblendungsprofil als Daten (Issue #915): Vorlage gültig, Fehler mit Stelle, keine Felder für Uhren."""

from __future__ import annotations

from typing import Any

import pytest

from hub.live.profil import FELDER, VORLAGEN, Box, ProfilError, lade_profil, pruefe_profil, vorlage


def test_vorlage_ist_gueltig_und_wird_kopiert() -> None:
    for name in VORLAGEN:
        assert pruefe_profil(vorlage(name)) == []
    profil = lade_profil(vorlage("balken_unten_dreizeilig"))
    assert set(profil.felder) == {"top", "name", "fraktion", "titel"}
    assert profil.bestaetigungen == 2
    assert "Oberbürgermeisterin" in profil.funktionen
    kopie = vorlage("balken_unten_dreizeilig")
    kopie["felder"]["top"]["psm"] = 13
    assert VORLAGEN["balken_unten_dreizeilig"]["felder"]["top"]["psm"] == 7, "Vorlage bleibt unverändert"


def test_box_rechnet_relativ_zur_bildgroesse() -> None:
    assert Box(0.5, 0.5, 1.0, 1.0).pixel(1920, 1080) == (960, 540, 1920, 1080)
    assert Box(0.5, 0.5, 1.0, 1.0).pixel(1280, 720) == (640, 360, 1280, 720)


def _mit(**aenderungen: Any) -> dict[str, Any]:
    daten = vorlage("balken_unten_dreizeilig")
    daten.update(aenderungen)
    return daten


@pytest.mark.parametrize(
    ("daten", "stelle"),
    [
        (_mit(version=2), "version"),
        (
            _mit(balken={"box": [0.5, 0.5, 0.4, 0.9], "farbregeln": [["b", "r", 25]], "mindestanteil": 0.3}),
            "balken.box",
        ),
        (_mit(balken={"box": [0, 0, 1, 1], "farbregeln": [["x", "r", 25]], "mindestanteil": 0.3}), "balken.farbregeln"),
        (_mit(balken={"box": [0, 0, 1, 1], "farbregeln": [["b", "r", 25]], "mindestanteil": 0}), "mindestanteil"),
        (_mit(top_muster="TOP (\\d+) (\\d+)"), "top_muster"),
        (_mit(top_muster="TOP ("), "top_muster"),
        (_mit(funktionen="Bürgermeister"), "funktionen"),
        (_mit(bestaetigungen=0), "bestaetigungen"),
        (_mit(felder={"name": {"box": [0, 0, 1, 1]}}), "felder.top"),
    ],
)
def test_ungueltige_profile_nennen_die_stelle(daten: dict[str, Any], stelle: str) -> None:
    probleme = pruefe_profil(daten)
    assert probleme
    assert any(stelle in p for p in probleme), probleme


def test_uhren_und_redezeiten_sind_kein_feld() -> None:
    """Redezeiten werden nie gelesen: Ein Profil kann kein solches Feld anlegen."""
    assert "redezeit" not in FELDER and "uhr" not in FELDER
    daten = vorlage("balken_unten_dreizeilig")
    daten["felder"]["redezeit"] = {"box": [0.9, 0.9, 1.0, 1.0]}
    with pytest.raises(ProfilError) as fehler:
        lade_profil(daten)
    assert any("redezeit" in p for p in fehler.value.probleme)


def test_feldangaben_werden_geprueft() -> None:
    daten = vorlage("balken_unten_dreizeilig")
    daten["felder"]["titel"]["psm"] = 99
    daten["felder"]["name"]["schwelle"] = 300
    daten["felder"]["fraktion"]["farbe"] = "rot"
    probleme = pruefe_profil(daten)
    assert any("felder.titel.psm" in p for p in probleme)
    assert any("felder.name.schwelle" in p for p in probleme)
    assert any("felder.fraktion" in p and "unbekannte" in p for p in probleme)


def test_kein_objekt() -> None:
    assert pruefe_profil([]) == ["Profil: Objekt erwartet"]
    assert pruefe_profil(None)
