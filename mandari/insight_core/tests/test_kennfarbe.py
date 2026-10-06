# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennfarbe als Token (Issue #783, Stufe 1): ``primary-*`` kommt aus CSS-Variablen im Kanalformat. Work und
Session behalten Indigo, das Bürgerportal ist Grün, Körperschaftsportale erzeugen aus ihrer Akzentfarbe eine
eigene Skala mit geprüften Kontrasten.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from insight_core.farbskala import DUNKEL, MINDESTKONTRAST, STUFEN, WEISS, kontrast, skala
from insight_core.models import OParlBody, OParlSource

BASIS = Path(settings.BASE_DIR)
GRUEN_600 = "23 112 63"


def _rgb(kanal: str) -> tuple[int, int, int]:
    r, g, b = (int(teil) for teil in kanal.split())
    return (r, g, b)


def _variablen(css: str, selektor: str) -> dict[int, str]:
    block = re.search(re.escape(selektor) + r"\s*\{(.*?)\}", css, re.S)
    assert block, f"Block {selektor} fehlt"
    return {int(stufe): wert.strip() for stufe, wert in re.findall(r"--primary-(\d+):\s*([\d ]+);", block.group(1))}


class TestSkala:
    @pytest.mark.parametrize("farbe", ["#17703f", "#0f766e", "#9d174d", "#ffd700", "#ffff00", "#ffffff", "#000000"])
    def test_kontraste_der_genutzten_stufen(self, farbe: str) -> None:
        stufen = {stufe: _rgb(kanal) for stufe, kanal in skala(farbe)}
        assert tuple(stufen) == STUFEN
        assert kontrast(WEISS, stufen[600]) >= MINDESTKONTRAST, "weißer Text auf dem Hauptknopf"
        assert kontrast(stufen[700], stufen[50]) >= MINDESTKONTRAST, "Text auf heller Fläche"
        assert kontrast(stufen[400], DUNKEL) >= MINDESTKONTRAST, "Text im dunklen Modus"

    def test_dunkle_akzentfarbe_bleibt_stufe_600(self) -> None:
        assert dict(skala("#17703f"))[600] == GRUEN_600

    def test_helle_akzentfarbe_wird_abgedunkelt_und_behaelt_den_farbton(self) -> None:
        r, g, b = _rgb(dict(skala("#ffd700"))[600])
        assert r > g > b, "Gelb bleibt gelblich"
        assert kontrast(WEISS, (r, g, b)) >= MINDESTKONTRAST

    def test_kanalformat(self) -> None:
        for _, kanal in skala("#4f46e5"):
            assert all(0 <= int(teil) <= 255 for teil in kanal.split()) and len(kanal.split()) == 3

    def test_ungueltige_farbe(self) -> None:
        with pytest.raises(ValueError):
            skala("rot")


class TestToken:
    def test_tailwind_liest_kanaele(self) -> None:
        config = (BASIS / "tailwind.config.js").read_text(encoding="utf-8")
        assert "rgb(var(--primary-${stufe}) / <alpha-value>)" in config
        assert "#4f46e5" not in config

    def test_indigo_standard_und_gruen_im_buergerportal(self) -> None:
        css = (BASIS / "static" / "css" / "input.css").read_text(encoding="utf-8")
        indigo = _variablen(css, ":root")
        gruen = _variablen(css, '[data-portal="insight"]')
        assert tuple(indigo) == STUFEN and tuple(gruen) == STUFEN
        assert indigo[600] == "79 70 229"
        assert gruen[600] == GRUEN_600
        assert kontrast(WEISS, _rgb(gruen[600])) >= MINDESTKONTRAST
        assert kontrast(WEISS, _rgb(gruen[500])) >= MINDESTKONTRAST
        assert kontrast(_rgb(gruen[400]), DUNKEL) >= MINDESTKONTRAST

    @pytest.mark.parametrize("vorlage", ["pages/map.html", "pages/meetings/calendar.html", "base_insight.html"])
    def test_keine_festen_indigo_werte_im_buergerportal(self, vorlage: str) -> None:
        text = (BASIS / "templates" / vorlage).read_text(encoding="utf-8").lower()
        for wert in ("#6366f1", "#4f46e5", "#4338ca", "#818cf8", "99,102,241", "99, 102, 241"):
            assert wert not in text, wert

    @pytest.mark.parametrize(
        "vorlage",
        [
            "work/base_work.html",
            "work/base_work_neu.html",
            "work/base_work_alt.html",
            "session/base_session.html",
            "base.html",
        ],
    )
    def test_work_und_session_bleiben_indigo(self, vorlage: str) -> None:
        """Nur das Bürgerportal setzt die Kennung; Work, Session und Konto erben Indigo aus ``:root``."""
        text = (BASIS / "templates" / vorlage).read_text(encoding="utf-8")
        assert "data-portal" not in text
        assert "--primary-" not in text

    def test_fehlerseite_der_dokumentansicht_ohne_indigo(self) -> None:
        text = (BASIS / "insight_core" / "views" / "files.py").read_text(encoding="utf-8").lower()
        assert "#4f46e5" not in text


@pytest.mark.django_db
class TestSeite:
    def _kommune(self, **felder: Any) -> OParlBody:
        source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
        return OParlBody.objects.create(
            external_id="https://ris.example.org/body/1", source=source, name="Beispielstadt", **felder
        )

    def test_buergerportal_traegt_die_kennung_und_gruen(self, client: Client) -> None:
        body = self._kommune(slug="beispiel")
        client.get(reverse("insight_core:insight:set_body", args=[body.id]))
        html = client.get(reverse("insight_core:insight:paper_list")).content.decode()
        assert 'data-portal="insight"' in html
        assert '<meta name="theme-color" content="#17703f">' in html
        assert "--primary-600:" not in html, "ohne Akzentfarbe gilt die Skala aus input.css"

    def test_koerperschaftsportal_setzt_eigene_skala(self, client: Client) -> None:
        body = self._kommune(slug="nord", accent_color="#0f766e")
        html = client.get(reverse("insight_core:insight:portal_entry", kwargs={"slug": "nord"}), follow=True)
        seite = html.content.decode()
        erwartet = dict(skala("#0f766e"))
        assert f"--primary-600: {erwartet[600]};" in seite
        assert f"--primary-50: {erwartet[50]};" in seite
        assert body.accent_color == "#0f766e"
