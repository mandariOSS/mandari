# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesung eines Einzelbilds nach Profil (Issue #915) mit synthetischen Bildern.

Die Feldauswertung (TOP-Nummer, Rauschen, Funktion statt Fraktion) prüft ein Ersatz für Tesseract; der
Integrationstest mit echtem Tesseract läuft nur, wo das Programm vorhanden ist (CI und Image haben es).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PIL import Image

from hub.live import ocr
from hub.live.lesung import (
    KEINE_EINBLENDUNG,
    balken_sichtbar,
    feldbild,
    lies_bild,
    nur_text,
    top_nummer,
    trenne_funktion,
    vereinfacht,
)
from hub.live.ocr import Erkenner
from hub.live.profil import STANDARD_FUNKTIONEN, TOP_ZEICHEN, lade_profil, vorlage
from hub.live.tests.bilder import einblendung

PROFIL = lade_profil(vorlage("balken_unten_dreizeilig"))


def _erkenner(*texte: str, zeichen: list[str] | None = None) -> tuple[list[int], Erkenner]:
    """
    Ersatz für Tesseract: gibt die Texte in der Reihenfolge der Felder zurück (top, name, fraktion, titel); die
    erlaubten Zeichen je Aufruf landen in zeichen.
    """
    aufrufe: list[int] = []
    folge: Iterator[str] = iter(texte)

    def erkennen(bild: Image.Image, psm: int, erlaubt: str) -> str:
        assert bild.mode == "L", "zweifarbiges Graustufenbild"
        aufrufe.append(psm)
        if zeichen is not None:
            zeichen.append(erlaubt)
        return next(folge)

    return aufrufe, erkennen


def test_balken_erkennung() -> None:
    assert balken_sichtbar(einblendung(), PROFIL.balken)
    assert not balken_sichtbar(einblendung(balken=False), PROFIL.balken)
    # andere Auflösung, gleiches Layout
    assert balken_sichtbar(einblendung(groesse=(1280, 720)), PROFIL.balken)


def test_ohne_balken_wird_nichts_gelesen() -> None:
    aufrufe, erkennen = _erkenner()
    assert lies_bild(einblendung(balken=False), PROFIL, erkennen) is KEINE_EINBLENDUNG
    assert aufrufe == []


def test_felder_in_reihenfolge_mit_psm_des_profils() -> None:
    aufrufe, erkennen = _erkenner("TOP 5.1\n", "Erika Muster\n", "Fraktion A\n", "Haushaltssatzung\n2027\nweiter\nmehr")
    lesung = lies_bild(einblendung(), PROFIL, erkennen)
    assert aufrufe == [7, 7, 7, 6]
    assert lesung.balken
    assert lesung.top == "5.1"
    assert lesung.name == "Erika Muster"
    assert lesung.fraktion == "Fraktion A"
    assert lesung.funktion is None
    assert lesung.titel == "Haushaltssatzung 2027 weiter", "höchstens drei Zeilen"


def test_top_feld_nur_mit_ziffern_punkt_und_top() -> None:
    """Das TOP-Feld liest nur Ziffern, Punkt und „TOP“ (sonst verliert Tesseract den Punkt), die übrigen alles."""
    zeichen: list[str] = []
    _, erkennen = _erkenner("TOP1.1", "Erika Muster", "Fraktion A", "Titel", zeichen=zeichen)
    assert lies_bild(einblendung(), PROFIL, erkennen).top == "1.1"
    assert zeichen == [TOP_ZEICHEN, "", "", ""]
    assert TOP_ZEICHEN == "0123456789.TOP"


def test_tesseract_bekommt_die_erlaubten_zeichen() -> None:
    assert ocr._befehl(7, TOP_ZEICHEN)[-2:] == ["-c", "tessedit_char_whitelist=0123456789.TOP"]
    assert not any("whitelist" in teil for teil in ocr._befehl(7))


def test_funktion_statt_fraktion() -> None:
    _, erkennen = _erkenner("TOP 3", "Max Beispiel", "Oberbürgermeister", "Titel")
    lesung = lies_bild(einblendung(), PROFIL, erkennen)
    assert (lesung.fraktion, lesung.funktion) == (None, "Oberbürgermeister")


def test_kurzer_name_ist_rauschen_und_ohne_namen_keine_fraktion() -> None:
    aufrufe, erkennen = _erkenner("—", "Ab", "Titel")
    lesung = lies_bild(einblendung(), PROFIL, erkennen)
    assert lesung.top is None and lesung.name is None and lesung.fraktion is None
    assert len(aufrufe) == 3, "Fraktion wird ohne Namen nicht gelesen"


@pytest.mark.parametrize(
    ("text", "nummer"),
    [("TOP 5", "5"), ("T0P: 12", "12"), ("TOP 1 . 1", "1.1"), ("top5", "5"), ("Tagesordnung", None), ("", None)],
)
def test_top_nummer(text: str, nummer: str | None) -> None:
    assert top_nummer(text, PROFIL) == nummer


@pytest.mark.parametrize(
    ("text", "fraktion", "funktion"),
    [
        ("Fraktion A", "Fraktion A", None),
        ("Bürgermeisterin", None, "Bürgermeisterin"),
        ("Beigeordneter für Planung", None, "Beigeordneter für Planung"),
        ("Stadtkämmerin", None, "Stadtkämmerin"),
        ("Stadtkämmerer", None, "Stadtkämmerer"),
        # unscharf: verlesene Umlaute und Buchstaben, Groß-/Kleinschreibung
        ("Stadtkammerer", None, "Stadtkammerer"),
        ("STADTKÄMMERIN", None, "STADTKÄMMERIN"),
        ("Oberburgermeister", None, "Oberburgermeister"),
        ("Oberb�rgermeisterin", None, "Oberb�rgermeisterin"),
        ("Bürgermeistern", None, "Bürgermeistern"),
        ("Verwaltung", None, "Verwaltung"),
        ("Verwaltungsreform-Liste", "Verwaltungsreform-Liste", None),
        (None, None, None),
    ],
)
def test_trenne_funktion(text: str | None, fraktion: str | None, funktion: str | None) -> None:
    assert trenne_funktion(text, STANDARD_FUNKTIONEN) == (fraktion, funktion)


def test_nur_text_und_vergleichsform() -> None:
    assert nur_text("  Erika Muster | \n") == "Erika Muster"
    assert nur_text("|| ~ 1") is None
    assert vereinfacht("Bündnis-Fraktion Süd") == "bundnis fraktion sud"


def test_feldbild_zweifarbig_und_vergroessert() -> None:
    feld = PROFIL.felder["name"]
    teil = feldbild(einblendung(), feld)
    links, oben, rechts, unten = feld.box.pixel(1920, 1080)
    assert teil.size == ((rechts - links) * 3, (unten - oben) * 3)
    assert set(teil.histogram()[1:255]) == {0}, "nur Schwarz und Weiß"


@pytest.mark.skipif(not ocr.sprache_verfuegbar(), reason="Tesseract mit Sprachdaten deu nicht installiert")
def test_lesung_mit_tesseract() -> None:
    """Integrationstest: echtes Tesseract liest Nummer und Namen aus einem gezeichneten Balken."""
    lesung = lies_bild(einblendung(top="TOP 3.1", name="Erika Muster", fraktion="Fraktion A"), PROFIL)
    assert lesung.balken
    assert lesung.top == "3.1"
    assert lesung.name == "Erika Muster"
    assert lesung.fraktion == "Fraktion A"


@pytest.mark.skipif(not ocr.sprache_verfuegbar(), reason="Tesseract mit Sprachdaten deu nicht installiert")
@pytest.mark.parametrize("top", ["TOP 1.1", "TOP 1.2", "TOP 11", "TOP 12.3"])
def test_tesseract_behaelt_den_punkt_und_den_ersten_buchstaben(top: str) -> None:
    """Titel beginnt knapp links der alten Grenze 0.555: Mit dem Ausschnitt ab 0.545 fehlt der erste Buchstabe nicht."""
    bild = einblendung(top=top, titel="Neubau einer Grundschule", titel_links=0.548)
    lesung = lies_bild(bild, PROFIL)
    assert lesung.top == top.removeprefix("TOP ")
    assert (lesung.titel or "").startswith("Neubau"), lesung.titel
