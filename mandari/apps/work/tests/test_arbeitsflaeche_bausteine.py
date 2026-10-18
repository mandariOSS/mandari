# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Bausteine der Arbeitsflächen in Work (Review zu #874, Bestandsaufnahme #951).

Sitzungsvorbereitung (#856) und Sitzungsansicht der laufenden Fraktionssitzung (#874) nutzen dieselben Knöpfe,
Menüs, Reiter, Tagesordnungszeilen, „Im Beratungsverlauf“, Aufgabenlisten, Werkzeugleisten, Leiste unten und Dialoge
(static/css/work-flaeche.css, Präfix wf-; Cotton-Bausteine c-work.*). Die Seiten bauen sie nicht noch einmal nach.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import engines
from django_cotton.compiler_regex import CottonCompiler

BASIS = Path(settings.BASE_DIR)
CSS = BASIS / "static" / "css"
SEITEN_CSS = {"work-vorbereitung.css": "vb", "fraktionssitzung.css": "fs"}
SEITEN_TEMPLATES = [
    *(BASIS / "templates" / "work" / "meetings" / "vorbereitung").glob("*.html"),
    *(BASIS / "templates" / "work" / "faction" / "sitzung").glob("*.html"),
]

#: Bausteine, die es nur noch gemeinsam gibt: Name ohne Präfix (vb-…, fs-…, wf-…)
GEMEINSAM = [
    "knopf",
    "textlink",
    "eingabe",
    "auswahl",
    "punkt",
    "menue",
    "menue-anker",
    "menue-liste",
    "reiter-liste",
    "reiterleiste",
    "tl-zeile",
    "tl-gruppe",
    "liste-gruppe",
    "top-h",
    "top-nr",
    "verlauf",
    "verlauf-h",
    "aufgaben",
    "auf-liste",
    "aufgabe-haken",
    "haken",
    "aufgabe-neu",
    "auf-neu",
    "wz-knopf",
    "wz-trenn",
    "leiste-nav",
    "nachbar",
    "dialog",
    "dialog-hg",
    "dialog-kopf",
    "dialog-fuss",
    "dok-treffer",
    "sr",
]


def _definierte_klassen(css: str) -> set[str]:
    """Klassen, die in Selektoren vorkommen (ohne Kommentare)."""
    ohne_kommentare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    selektoren = re.findall(r"([^{}]+)\{", ohne_kommentare)
    return {k for s in selektoren for k in re.findall(r"\.([a-zA-Z][\w-]*)", s)}


@pytest.mark.parametrize("datei,praefix", SEITEN_CSS.items())
def test_seiten_css_baut_keine_gemeinsamen_bausteine_nach(datei: str, praefix: str) -> None:
    klassen = _definierte_klassen((CSS / datei).read_text(encoding="utf-8"))
    doppelt = sorted(f"{praefix}-{name}" for name in GEMEINSAM if f"{praefix}-{name}" in klassen)
    assert not doppelt, f"{datei} definiert gemeinsame Bausteine selbst: {doppelt} (gehören nach work-flaeche.css)"
    # Positionspunkt hieß in der Vorbereitung nur „punkt“
    assert "punkt" not in klassen
    # Farben kommen aus der gemeinsamen Datei (--wf-…), nicht aus eigenen Sätzen je Seite
    assert f"--{praefix}-tinte" not in (CSS / datei).read_text(encoding="utf-8")


def test_gemeinsame_datei_steht_vor_den_seiten() -> None:
    klassen = _definierte_klassen((CSS / "work-flaeche.css").read_text(encoding="utf-8"))
    for name in ("knopf", "textlink", "menue", "reiter", "tl-zeile", "verlauf", "aufgaben", "nachbar", "dialog"):
        assert f"wf-{name}" in klassen, name
    eingang = (CSS / "input.css").read_text(encoding="utf-8")
    reihenfolge = [eingang.index(f'@import "./{d}"') for d in ("work-flaeche.css", *SEITEN_CSS)]
    assert reihenfolge == sorted(reihenfolge), "work-flaeche.css muss vor den Seiten stehen (Seiten ergänzen sie)"


@pytest.mark.parametrize("pfad", SEITEN_TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_seiten_nutzen_die_gemeinsamen_klassen(pfad: Path) -> None:
    text = pfad.read_text(encoding="utf-8")
    alt = sorted(
        {
            f"{praefix}-{name}"
            for praefix in SEITEN_CSS.values()
            for name in GEMEINSAM
            if re.search(rf'class="[^"]*(?<![\w-]){praefix}-{name}(?![\w-])', text)
        }
    )
    assert not alt, f"{pfad.name} nutzt noch eigene Bausteine: {alt}"


def _rendern(quelle: str, **kontext: object) -> str:
    vorlage = engines["django"].from_string(CottonCompiler().process("{% load cotton %}" + quelle))
    return vorlage.render(kontext)


def test_beratungsverlauf_baustein() -> None:
    html = _rendern(
        '<c-work.beratungsverlauf kennung="v-h" x-show="offen && liste.length > 0">'
        "<ul>"
        '<c-work.verlauf-eintrag wo="Bau & Verkehr, 06.10.2026" href="/vorbereitung/" punkt="zustimmung" '
        'position="Zustimmung" titel="Bau & Verkehr: Zustimmung">, endgültig</c-work.verlauf-eintrag>'
        '<c-work.verlauf-eintrag wo="Rat, 17.10.2026" punkt="andere" position="Mit Änderungsantrag" />'
        "</ul></c-work.beratungsverlauf>"
    )
    assert '<div class="wf-verlauf" role="group" aria-labelledby="v-h" x-show="offen && liste.length > 0">' in html
    assert '<span class="wf-verlauf-h" id="v-h">' in html and "Im Beratungsverlauf</span>" in html
    assert (
        '<li title="Bau &amp; Verkehr: Zustimmung"><a class="wo" href="/vorbereitung/">Bau &amp; Verkehr, 06.10.2026</a>: '
        '<span class="wf-punkt zustimmung" aria-hidden="true"></span><b>Zustimmung</b>, endgültig</li>'
    ) in html
    # Ohne Adresse kein Link
    assert '<span class="wo">Rat, 17.10.2026</span>: <span class="wf-punkt andere"' in html
