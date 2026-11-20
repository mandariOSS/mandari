# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Briefkopf im PDF-Export: Der Speicherbedarf hängt nicht am hochgeladenen PDF.

- Ein Briefkopf, dessen Seiteninhalt entpackt über der Grenze liegt, wird nicht zusammengeführt; der Export liefert
  das Inhalts-PDF. Gemessen wird in Stücken, ohne den Inhalt im Speicher aufzubauen.
- Je Prozess führt nur ein Export zugleich einen Briefkopf zusammen.
- Gewöhnliche Briefköpfe liegen weiter unter dem Inhalt.
"""

from __future__ import annotations

import base64
import io
import threading
import zlib
from typing import Any
from unittest import mock

import pytest
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, RectangleObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from apps.common import pdf


def _seite(*texte: str) -> bytes:
    """PDF mit einer Seite je Text."""
    puffer = io.BytesIO()
    blatt = canvas.Canvas(puffer, pagesize=A4)
    for text in texte:
        blatt.drawString(72, 720, text)
        blatt.showPage()
    blatt.save()
    return puffer.getvalue()


def _mit_seiteninhalt(*stroeme: Any) -> bytes:
    """Einseitiges PDF, dessen Seiteninhalt aus den übergebenen Strömen besteht."""
    writer = PdfWriter()
    seite = writer.add_blank_page(width=595, height=842)
    verweise = [writer._add_object(strom) for strom in stroeme]
    seite[NameObject("/Contents")] = verweise[0] if len(verweise) == 1 else ArrayObject(verweise)
    puffer = io.BytesIO()
    writer.write(puffer)
    return puffer.getvalue()


def _strom(inhalt: bytes, *, gepackt: bool = True) -> Any:
    strom = DecodedStreamObject()
    strom.set_data(inhalt)
    return strom.flate_encode() if gepackt else strom


def _text(daten: bytes) -> str:
    return "\n".join(seite.extract_text() for seite in PdfReader(io.BytesIO(daten)).pages)


INHALT = _seite("Antrag der Fraktion")


def test_gewoehnlicher_briefkopf_liegt_unter_dem_inhalt() -> None:
    ergebnis = pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(_seite("Fraktion im Rat der Stadt")))

    assert ergebnis != INHALT
    text = _text(ergebnis)
    assert "Fraktion im Rat der Stadt" in text and "Antrag der Fraktion" in text


def test_jede_seite_hat_briefkopf_und_nur_ihren_eigenen_inhalt() -> None:
    """Mehrseitiger Export (#1464): pypdf 6 schrieb den Inhalt der ersten Seite auf alle, pypdf 7 brach ab."""
    inhalt = _seite("Seite Eins", "Seite Zwei", "Seite Drei")

    ergebnis = pdf.overlay_pdf_letterhead(inhalt, io.BytesIO(_seite("Fraktion im Rat der Stadt")))

    seiten = [" ".join(seite.extract_text().split()) for seite in PdfReader(io.BytesIO(ergebnis)).pages]
    assert seiten == [
        "Fraktion im Rat der Stadt Seite Eins",
        "Fraktion im Rat der Stadt Seite Zwei",
        "Fraktion im Rat der Stadt Seite Drei",
    ]


def test_seitenformat_kommt_vom_briefkopf() -> None:
    """Auch ein Briefkopf, dessen Seite nicht bei null beginnt, bleibt mit seinem Format und Ausschnitt erhalten."""
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_seite("Fraktion im Rat der Stadt"))))
    writer.pages[0].mediabox = RectangleObject([10, 20, 605, 862])
    writer.pages[0].cropbox = RectangleObject([20, 30, 595, 852])
    puffer = io.BytesIO()
    writer.write(puffer)

    ergebnis = PdfReader(io.BytesIO(pdf.overlay_pdf_letterhead(INHALT, puffer))).pages[0]

    assert [float(wert) for wert in ergebnis.mediabox] == [10, 20, 605, 862]
    assert [float(wert) for wert in ergebnis.cropbox] == [20, 30, 595, 852]
    assert "Fraktion im Rat der Stadt" in ergebnis.extract_text()


def test_zu_grosser_seiteninhalt_wird_nicht_zusammengefuehrt() -> None:
    """Wenige Kilobyte in der Datei, entpackt über der Grenze: Der Export bleibt beim Inhalts-PDF."""
    briefkopf = _mit_seiteninhalt(_strom(b"q Q\n" * (pdf.LETTERHEAD_MAX_CONTENT_BYTES // 4 + 1024)))
    assert len(briefkopf) < 64 * 1024

    with mock.patch.object(PageObject, "merge_page") as zusammenfuehren:
        ergebnis = pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(briefkopf))

    assert ergebnis == INHALT
    zusammenfuehren.assert_not_called()


def test_grenze_gilt_fuer_alle_stroeme_der_seite_zusammen() -> None:
    teil = b"q Q\n" * (pdf.LETTERHEAD_MAX_CONTENT_BYTES // 8 + 1024)
    briefkopf = _mit_seiteninhalt(_strom(teil), _strom(teil, gepackt=False))

    assert pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(briefkopf)) == INHALT


def test_grenze_gilt_auch_hinter_einer_textkodierung() -> None:
    """Seiteninhalt als ASCII85 um Flate, wie ihn etwa reportlab schreibt."""
    gepackt = zlib.compress(b"q Q\n" * (pdf.LETTERHEAD_MAX_CONTENT_BYTES // 4 + 1024))
    strom = DecodedStreamObject()
    strom.set_data(base64.a85encode(gepackt) + b"~>")
    strom[NameObject("/Filter")] = ArrayObject([NameObject("/ASCII85Decode"), NameObject("/FlateDecode")])
    briefkopf = _mit_seiteninhalt(strom)

    with mock.patch.object(PageObject, "merge_page") as zusammenfuehren:
        ergebnis = pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(briefkopf))

    assert ergebnis == INHALT
    zusammenfuehren.assert_not_called()


def test_laenge_wird_gezaehlt_ohne_alles_zu_entpacken() -> None:
    """Die Zählung bricht kurz hinter der Grenze ab, gleich wie viel im Strom steckt."""
    seite = PdfReader(io.BytesIO(_mit_seiteninhalt(_strom(b"\x00" * (64 * 1024 * 1024))))).pages[0]

    laenge = pdf._page_content_length(seite, 1024 * 1024)

    assert laenge is not None and 1024 * 1024 < laenge <= 1024 * 1024 + 64 * 1024


def test_nicht_messbare_kodierung_wird_nicht_zusammengefuehrt() -> None:
    strom = _strom(b"q Q\n", gepackt=False)
    strom[NameObject("/Filter")] = NameObject("/LZWDecode")
    briefkopf = _mit_seiteninhalt(strom)

    with mock.patch.object(PageObject, "merge_page") as zusammenfuehren:
        ergebnis = pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(briefkopf))

    assert ergebnis == INHALT
    zusammenfuehren.assert_not_called()


def test_nur_ein_export_zugleich_fuehrt_zusammen(monkeypatch: pytest.MonkeyPatch) -> None:
    briefkopf = _seite("Fraktion im Rat der Stadt")
    begonnen, weiter = threading.Event(), threading.Event()
    gleichzeitig: list[int] = []
    laufend = 0
    echt = PageObject.merge_page

    def langsam(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal laufend
        laufend += 1
        gleichzeitig.append(laufend)
        begonnen.set()
        weiter.wait(5)
        try:
            return echt(self, *args, **kwargs)
        finally:
            laufend -= 1

    monkeypatch.setattr(PageObject, "merge_page", langsam)
    ergebnisse: list[bytes] = []
    exporte = [
        threading.Thread(target=lambda: ergebnisse.append(pdf.overlay_pdf_letterhead(INHALT, io.BytesIO(briefkopf))))
        for _ in range(3)
    ]
    for export in exporte:
        export.start()
    assert begonnen.wait(5)
    weiter.set()
    for export in exporte:
        export.join(10)

    assert len(ergebnisse) == 3 and all(ergebnis != INHALT for ergebnis in ergebnisse)
    assert max(gleichzeitig) == 1


def test_briefkopf_wird_vor_der_sperre_aus_der_ablage_gelesen() -> None:
    """Ein langsamer Abruf aus der Ablage hält die Exporte der anderen nicht auf: Gelesen wird ohne die Sperre."""
    gesperrt_beim_lesen: list[bool] = []

    class Ablage(io.BytesIO):
        def read(self, *args: Any) -> bytes:
            gesperrt_beim_lesen.append(pdf._LETTERHEAD_LOCK.locked())
            return super().read(*args)

    ablage = Ablage(_seite("Fraktion im Rat der Stadt"))
    ablage.seek(0, io.SEEK_END)  # die Stelle, an der das Objekt gerade steht, spielt keine Rolle

    ergebnis = pdf.overlay_pdf_letterhead(INHALT, ablage)

    assert "Fraktion im Rat der Stadt" in _text(ergebnis)
    assert gesperrt_beim_lesen and not any(gesperrt_beim_lesen)
