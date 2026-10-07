# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentansicht ohne Lade- und Fehlertexte im Seitentext (Issue #914, Soft-404).

Jede Seite von Insight (und von Work) bindet die Dokumentansicht ``components/document_viewer.html`` verborgen ein.
Ihre Lade- und Fehlertexte standen bisher als Text im ausgelieferten HTML. Ein SEO-Crawler stufte Vorgänge und
Sitzungen deshalb als Fehlerseiten (Soft-404) ein; Suchmaschinen werten solche Formulierungen im Seitentext ähnlich.

Die Texte stehen jetzt nur noch in ``data-text``-Attributen und kommen erst im jeweiligen Zustand per ``x-text``
ins Element. Geprüft wird streng: Auch in ``<template>`` dürfen sie nicht als Text stehen, denn einfache Crawler
lesen dessen Inhalt mit. Das gilt ebenso für „noch nicht verfügbar“ im Kommunenwechsel, der als Dialog in jeder
Seite steckt. Das Verhalten im Browser (Ladezustand, Fehlerfall) prüft ``tests_e2e/test_dokumentansicht.py``.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

RIS = "https://ris.soft404.example/oparl"

#: Lade- und Fehlertexte der Dokumentansicht – im Browser weiter da, im HTML kein Seitentext
ZUSTANDSTEXTE = (
    "Dokument wird geladen …",
    "Vorschau nicht verfügbar",
    "Das Dokument kann nicht eingebettet angezeigt werden.",
    "Dokument öffnen",
)
#: Wortteile, an denen auch abgewandelte Fassungen (z. B. „Dokument wird geladen...“) auffallen
VERBOTEN_IM_TEXT = ("wird geladen", "nicht verfügbar", "nicht eingebettet", "Dokument öffnen")


class _Seitentext(HTMLParser):
    """Sammelt alle Textknoten außer in ``<script>`` und ``<style>`` (``<template>``-Inhalt zählt mit)."""

    OHNE = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ohne = 0
        self.texte: list[str] = []
        self.data_texte: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.OHNE:
            self._ohne += 1
        self.data_texte.extend(wert or "" for name, wert in attrs if name == "data-text")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.OHNE and self._ohne:
            self._ohne -= 1

    def handle_data(self, data: str) -> None:
        if not self._ohne and data.strip():
            self.texte.append(" ".join(data.split()))


def _lesen(html: str) -> _Seitentext:
    parser = _Seitentext()
    parser.feed(html)
    parser.close()
    return parser


def _ohne_zustandstexte(html: str) -> None:
    gelesen = _lesen(html)
    funde = [text for text in gelesen.texte if any(teil in text for teil in VERBOTEN_IM_TEXT)]
    assert not funde, f"Lade-/Fehlertexte als Seitentext: {funde}"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Soft-404-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Beispielstadt")


def _datei(body: OParlBody, name: str, **bezug: object) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"{RIS}/file/{name}",
        body=body,
        name=name,
        file_name=f"{name}.pdf",
        mime_type="application/pdf",
        access_url=f"https://ris.soft404.example/files/{name}.pdf",
        **bezug,
    )


class TestDetailseiten:
    def test_vorgang(self, body: OParlBody) -> None:
        paper = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/1", body=body, name="Sanierung des Stadtparks", reference="V/2026/001"
        )
        _datei(body, "Beschlussvorlage", paper=paper)
        response = Client().get(f"/insight/vorgaenge/{paper.id}/")
        assert response.status_code == 200
        html = response.content.decode()
        # Die Seite bietet die Dokumentansicht an („Ansehen“), der Betrachter ist eingebunden – ebenso der
        # Kommunenwechsel, dessen Zeilen für Kommunen ohne Daten „noch nicht verfügbar“ zeigen
        assert "openDoc(" in html and 'title="Dokumentvorschau"' in html
        assert 'x-data="kommunenWahl"' in html
        _ohne_zustandstexte(html)

    def test_sitzung(self, body: OParlBody) -> None:
        meeting = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1", body=body, name="Rat", start=timezone.now()
        )
        _datei(body, "Einladung", meeting=meeting)
        response = Client().get(f"/insight/termine/{meeting.id}/")
        assert response.status_code == 200
        html = response.content.decode()
        assert 'title="Dokumentvorschau"' in html
        _ohne_zustandstexte(html)


class TestBetrachter:
    """Die Komponente selbst – sie steckt auch in den Rahmen von Work (alt und neu)."""

    def test_texte_nur_als_daten(self) -> None:
        gelesen = _lesen(render_to_string("components/document_viewer.html"))
        funde = [text for text in gelesen.texte if any(teil in text for teil in VERBOTEN_IM_TEXT)]
        assert not funde, f"Lade-/Fehlertexte als Seitentext: {funde}"
        # Die Texte bleiben vollständig erhalten: Der Browser setzt sie im jeweiligen Zustand ein
        assert sorted(gelesen.data_texte) == sorted(ZUSTANDSTEXTE)

    def test_texte_haengen_am_zustand(self) -> None:
        html = render_to_string("components/document_viewer.html")
        # Ladetext nur beim Laden, Fehlertexte nur im Fehlerfall – sonst stünden sie nach dem Start von Alpine
        # wieder (verborgen) im Seitentext, den Suchmaschinen nach dem Rendern lesen
        assert html.count("x-text=\"docViewerLoading ? $el.dataset.text : ''\"") == 1
        assert html.count("x-text=\"docViewerError ? $el.dataset.text : ''\"") == 3
