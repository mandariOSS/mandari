# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Dokumentimport läuft im Webprozess: Kleine, präparierte Dateien dürfen ihn weder lange aufhalten noch seinen
Speicher füllen.

- Leerraum in einem Absatz wird in linearer Zeit abgeschnitten.
- Tabellen gehen über ihre tatsächlichen Zellen ein; die Spanne einer Zelle (``gridSpan``) steht ungeprüft in der
  Datei und darf weder Speicher noch Zeit kosten. Verbundene Zellen erscheinen wie bisher.
- Das entpackte Paket hat Obergrenzen, geprüft vor dem Lesen.
"""

from __future__ import annotations

import copy
import io
import time
import tracemalloc
import zipfile
from typing import Any

import docx
import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from apps.work.motions import import_text

ZEITGRENZE = 3.0


def _bytes(dokument: Any) -> bytes:
    puffer = io.BytesIO()
    dokument.save(puffer)
    return puffer.getvalue()


def _spanne(zelle: Any, wert: int) -> None:
    tc_pr = zelle._tc.get_or_add_tcPr()
    grid_span = OxmlElement("w:gridSpan")
    grid_span.set(qn("w:val"), str(wert))
    tc_pr.append(grid_span)


# --- Absatzränder -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "erwartet"),
    [
        ("  a  ", "a"),
        ("<br> <br>a<br>b <br>\n", "a<br>b"),
        ("a", "a"),
        ("", ""),
        (" <br> ", ""),
        ("<strong> a </strong>", "<strong> a </strong>"),
    ],
)
def test_absatzraender_wie_bisher(html: str, erwartet: str) -> None:
    assert import_text._trim_edge_breaks(html) == erwartet


def test_langer_leerraum_im_absatz_bleibt_linear() -> None:
    dokument = docx.Document()
    dokument.add_paragraph("a" + " " * 200_000 + "b")
    daten = _bytes(dokument)

    start = time.perf_counter()
    html = import_text.docx_to_html(daten)

    assert time.perf_counter() - start < ZEITGRENZE
    assert html.startswith("<p>a") and html.endswith("b</p>")


# --- Tabellen -----------------------------------------------------------------------------------------


def test_verbundene_zellen_erscheinen_wie_bisher() -> None:
    dokument = docx.Document()
    tabelle = dokument.add_table(rows=3, cols=3)
    for zeile in range(3):
        for spalte in range(3):
            tabelle.cell(zeile, spalte).text = f"z{zeile}s{spalte}"
    # Über zwei Spalten in der ersten Zeile, über zwei Zeilen in der letzten Spalte
    tabelle.cell(0, 0).merge(tabelle.cell(0, 1))
    tabelle.cell(1, 2).merge(tabelle.cell(2, 2))

    html = import_text.docx_to_html(_bytes(dokument))

    zeilen = html.split("<tr>")[1:]
    assert len(zeilen) == 3
    # Erste Zeile: die verbundene Zelle einmal, mit dem Text beider Zellen
    assert zeilen[0].count("<td>") == 2 and "z0s0" in zeilen[0] and "z0s1" in zeilen[0]
    # Die Folgezeile der senkrechten Verbindung zeigt den Inhalt der obersten Zelle
    assert zeilen[1].count("<td>") == 3 and "z1s2" in zeilen[1]
    assert zeilen[2].count("<td>") == 3 and "z1s2" in zeilen[2] and "z2s0" in zeilen[2]


def test_riesige_spanne_kostet_weder_speicher_noch_zeit() -> None:
    dokument = docx.Document()
    tabelle = dokument.add_table(rows=2, cols=2)
    tabelle.cell(0, 0).text = "Kopf"
    tabelle.cell(1, 1).text = "Inhalt"
    _spanne(tabelle.cell(0, 0), 2_000_000_000)
    daten = _bytes(dokument)

    tracemalloc.start()
    start = time.perf_counter()
    html = import_text.docx_to_html(daten)
    dauer = time.perf_counter() - start
    _aktuell, spitze = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert "Kopf" in html and "Inhalt" in html
    assert dauer < ZEITGRENZE
    assert spitze < 50 * 1024 * 1024


def test_tabelle_ueber_dem_zellenbudget_behaelt_ihren_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_text, "MAX_TABLE_CELLS", 5)
    dokument = docx.Document()
    tabelle = dokument.add_table(rows=3, cols=3)
    tabelle.cell(0, 0).text = "Anfang"
    tabelle.cell(2, 2).text = "Ende"

    html = import_text.docx_to_html(_bytes(dokument))

    assert "<table>" not in html
    assert "Anfang" in html and "Ende" in html


def test_wiederholter_inhalt_verbundener_zeilen_ist_begrenzt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eine große oberste Zelle mit vielen Fortsetzungen: Die Ausgabe wächst nicht mit Inhalt mal Zeilen."""
    monkeypatch.setattr(import_text, "MAX_TABLE_HTML_CHARS", 200_000)
    dokument = docx.Document()
    zeilen = 400
    tabelle = dokument.add_table(rows=zeilen, cols=2)
    tabelle.cell(0, 0).text = "Ratsbeschluss " * 2_000
    tabelle.cell(zeilen - 1, 1).text = "Ende"
    tabelle.cell(0, 0).merge(tabelle.cell(zeilen - 1, 0))
    daten = _bytes(dokument)

    tracemalloc.start()
    html = import_text.docx_to_html(daten)
    _aktuell, spitze = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert "<table>" not in html
    assert "Ratsbeschluss" in html and "Ende" in html
    assert len(html) < 100_000
    assert spitze < 50 * 1024 * 1024


def test_zeichenbudget_gilt_fuer_alle_tabellen_eines_dokuments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_text, "MAX_TABLE_HTML_CHARS", 1_000)
    dokument = docx.Document()
    for nummer in range(3):
        tabelle = dokument.add_table(rows=1, cols=1)
        tabelle.cell(0, 0).text = f"Tabelle {nummer} " + "x" * 600

    html = import_text.docx_to_html(_bytes(dokument))

    # Die erste Tabelle passt ins Budget, die weiteren bleiben als Text erhalten
    assert html.count("<table>") == 1
    assert all(f"Tabelle {nummer}" in html for nummer in range(3))


def test_viele_zellen_in_einer_zeile_bleiben_linear() -> None:
    dokument = docx.Document()
    tabelle = dokument.add_table(rows=1, cols=1)
    zeile = tabelle.rows[0]._tr
    vorlage = zeile.tc_lst[0]
    for _ in range(8_000):
        zeile.append(copy.deepcopy(vorlage))
    assert len(zeile.tc_lst) == 8_001
    tabelle.cell(0, 0).text = "Text"
    daten = _bytes(dokument)

    start = time.perf_counter()
    html = import_text.docx_to_html(daten)

    assert time.perf_counter() - start < 2 * ZEITGRENZE
    assert html.count("<td>") == 8_001


# --- Paketgröße ---------------------------------------------------------------------------------------


def _paket(teile: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
        for name, inhalt in teile.items():
            archiv.writestr(name, inhalt)
    return puffer.getvalue()


def test_gewoehnliches_dokument_besteht_die_paketpruefung() -> None:
    dokument = docx.Document()
    dokument.add_paragraph("Text")

    import_text.check_docx_package(_bytes(dokument))
    # Kein Archiv: Das meldet wie bisher python-docx, nicht diese Prüfung
    import_text.check_docx_package(b"kein Archiv")


@pytest.mark.parametrize(
    "teile",
    [
        {"word/document.xml": b" " * (26 * 1024 * 1024)},
        {"word/media/bild.bin": bytes(151 * 1024 * 1024)},
        {f"word/teil{i}.txt": b"x" for i in range(5_001)},
    ],
)
def test_zu_grosses_paket_wird_vor_dem_lesen_abgelehnt(teile: dict[str, bytes]) -> None:
    daten = _paket(teile)
    assert len(daten) < 2 * 1024 * 1024  # gepackt klein, entpackt groß

    with pytest.raises(import_text.DocxTooLargeError):
        import_text.docx_to_html(daten)
