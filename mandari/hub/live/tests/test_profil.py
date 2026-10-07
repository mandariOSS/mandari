# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einblendungsprofil als Daten (Issue #915): Vorlage gültig, Fehler mit Stelle, keine Felder für Uhren."""

from __future__ import annotations

from typing import Any

import pytest

from hub.live.profil import (
    FELDER,
    TITEL_ALLEIN,
    TITEL_ZUR_NUMMER,
    TOP_ZEICHEN,
    VORLAGEN,
    Box,
    ProfilError,
    lade_profil,
    pruefe_profil,
    vorlage,
)


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


def test_vorlage_titel_ab_0545_ohne_ueberlappung_mit_dem_top_feld() -> None:
    """Der Titel beginnt bei 0.545 (vorher fehlte der erste Buchstabe); das TOP-Feld ragt nicht hinein."""
    felder = lade_profil(vorlage("balken_unten_dreizeilig")).felder
    assert felder["titel"].box.links == 0.545
    assert felder["top"].box.rechts <= felder["titel"].box.links


def test_top_feld_liest_nur_ziffern_punkt_und_top() -> None:
    profil = lade_profil(vorlage("balken_unten_dreizeilig"))
    assert profil.felder["top"].zeichen == TOP_ZEICHEN
    assert {profil.felder[name].zeichen for name in ("name", "fraktion", "titel")} == {""}
    assert "zeichen" not in vorlage("balken_unten_dreizeilig")["felder"]["top"], "ältere Images kennen die Angabe nicht"
    # eigenes Muster (andere Beschriftung): ohne Einschränkung; ausdrücklich leer: ebenso
    assert lade_profil(_mit(top_muster="Punkt (\\d+)")).felder["top"].zeichen == ""
    daten = vorlage("balken_unten_dreizeilig")
    daten["felder"]["top"]["zeichen"] = ""
    assert lade_profil(daten).felder["top"].zeichen == ""
    daten["felder"]["top"]["zeichen"] = "0123456789."
    assert lade_profil(daten).felder["top"].zeichen == "0123456789."
    daten["felder"]["top"]["zeichen"] = "0 1'"
    assert any("felder.top.zeichen" in p for p in pruefe_profil(daten))


def test_schwellen_der_titelpruefung() -> None:
    profil = lade_profil(vorlage("balken_unten_dreizeilig"))
    assert (profil.titel_zur_nummer, profil.titel_allein) == (TITEL_ZUR_NUMMER, TITEL_ALLEIN) == (0.6, 0.75)
    assert lade_profil(_mit(titel_zur_nummer=0.5, titel_allein=0.9)).titel_allein == 0.9
    assert any("titel_allein" in p for p in pruefe_profil(_mit(titel_zur_nummer=0.8, titel_allein=0.7)))
    assert any("titel_zur_nummer" in p for p in pruefe_profil(_mit(titel_zur_nummer=1.5)))
    assert any("titel_allein" in p for p in pruefe_profil(_mit(titel_allein="hoch")))


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
