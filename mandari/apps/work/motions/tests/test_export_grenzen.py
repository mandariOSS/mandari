# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenzen für den Export eines Dokuments (PDF, DOCX).

Der Export läuft im Webprozess, der alle Anfragen bedient. Was ein Dokument enthält, darf ihn deshalb weder lange
aufhalten noch viel Speicher kosten:

- Die Vorbereitung des Inhalts läuft auch bei sehr langem Leerraum in linearer Zeit.
- Tabellenzellen überspannen höchstens ``MAX_TABLE_SPAN`` Spalten und Zeilen; der PDF-Export nimmt Tabellen nur bis
  ``MAX_PDF_TABLE_CELLS`` Felder an und lehnt größere mit einer festen Meldung ab.
- Eingebettete Bilder sind Rasterbilder, erkannt an ihrer Signatur, nicht an der angegebenen Bildart.
- All das gilt auch für Inhalt, der vor diesen Regeln gespeichert wurde: Der Export bereinigt ihn erneut.
"""

from __future__ import annotations

import base64
import time
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions import email_submission, export_service
from apps.work.motions.export_service import (
    MAX_PDF_TABLE_CELLS,
    PDF_TABLES_TOO_LARGE,
    ExportTooLargeError,
    clean_editor_html,
    motion_export_service,
    table_cell_count,
)
from apps.work.motions.models import Motion
from apps.work.motions.ris_submission import SubmissionError
from apps.work.sanitize import MAX_TABLE_SPAN, sanitize_editor_html

ZEITGRENZE = 3.0

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
VEKTOR = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'


def _bild(daten: bytes, art: str = "png") -> str:
    return f'<img src="data:image/{art};base64,{base64.b64encode(daten).decode("ascii")}">'


def _tabelle(zeilen: int, spalten: int, zelle: str = "<p>x</p>") -> str:
    return "<table><tbody>" + ("<tr>" + f"<td>{zelle}</td>" * spalten + "</tr>") * zeilen + "</tbody></table>"


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.edit"], email="autorin@example.org")


def _dokument(org: Any, autorin: Any, inhalt: str) -> Motion:
    """Dokument, dessen Inhalt ohne Bereinigung in der Datenbank steht (wie Altbestand)."""
    motion = Motion(organization=org, author=autorin, title="Antrag", visibility="organization")
    cast(Any, motion).set_content_encrypted(inhalt)
    motion.save()
    return motion


# --- Vorbereitung des Inhalts ------------------------------------------------------------------------------------


@pytest.mark.parametrize("leerraum", [" ", "\t", "\n", " \n"])
def test_vorbereitung_bleibt_linear_bei_langem_leerraum(leerraum: str) -> None:
    html = "<p>" + leerraum * (200_000 // len(leerraum)) + "</p>"

    start = time.perf_counter()
    ergebnis = clean_editor_html(html)

    assert time.perf_counter() - start < ZEITGRENZE
    assert ergebnis == html


def test_vorbereitung_entfernt_angaben_des_editors() -> None:
    html = (
        '<p data-type="taskItem" data-checked="true">Text '
        '<span data-comment-id="c-1" class="comment-mark">kommentiert</span> und '
        '<mark data-comment-id="c-2" class="comment-mark comment-mark--resolved">erledigt</mark></p>'
        '<div data-page-break="true" class="page-break"></div>'
    )

    assert clean_editor_html(html) == '<p>Text kommentiert und erledigt</p><div class="page-break"></div>'


def test_vorbereitung_laesst_fliesstext_unveraendert() -> None:
    """Nur Marken verlieren Attribute: Text, der wie ein Attribut aussieht, bleibt stehen."""
    html = '<p>Im Quelltext steht data-id="7" und class="comment-mark" als Beispiel.</p>'

    assert clean_editor_html(html) == html


# --- Tabellen ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("wert", ["1", "2", str(MAX_TABLE_SPAN)])
def test_uebliche_spannweiten_bleiben(wert: str) -> None:
    html = f'<table><tbody><tr><td colspan="{wert}" rowspan="{wert}"><p>x</p></td></tr></tbody></table>'

    assert sanitize_editor_html(html) == html


@pytest.mark.parametrize("wert", ["0", str(MAX_TABLE_SPAN + 1), "3000", "99999"])
def test_zu_grosse_spannweiten_entfallen(wert: str) -> None:
    html = f'<table><tbody><tr><th colspan="{wert}" rowspan="{wert}"><p>x</p></th></tr></tbody></table>'

    assert sanitize_editor_html(html) == "<table><tbody><tr><th><p>x</p></th></tr></tbody></table>"


def test_tabellenfelder_werden_je_tabelle_gezaehlt() -> None:
    # Zellen und Raster: 20 Zeilen mit 6 Spalten
    assert table_cell_count(_tabelle(20, 6)) == 240
    assert table_cell_count(_tabelle(20, 6) + "<p>Text</p>" + _tabelle(2, 3)) == 240 + 12
    assert table_cell_count("<p>ohne Tabelle</p>") == 0
    # Eine Zelle über 3 Spalten und 2 Zeilen belegt 6 Felder und macht beide Zeilen 4 Spalten breit
    verbunden = '<table><tbody><tr><td colspan="3" rowspan="2"></td><td></td></tr><tr><td></td></tr></tbody></table>'
    assert table_cell_count(verbunden) == (6 + 1 + 1) + 2 * 4


def test_spannweiten_aus_frueheren_zeilen_zaehlen_zur_breite() -> None:
    """Viele Zeilen mit je einer hohen Zelle: Jede reicht in die folgenden und verbreitert deren Raster."""
    hoch = f'<tr><td rowspan="{MAX_TABLE_SPAN}"></td></tr>' * MAX_TABLE_SPAN
    html = "<table><tbody>" + hoch + "<tr><td></td></tr>" * 1000 + "</tbody></table>"

    # Breiteste Zeile: die eigene Zelle und die 63 Zellen der Zeilen davor
    assert table_cell_count(html) == MAX_TABLE_SPAN * MAX_TABLE_SPAN + 1000 + (MAX_TABLE_SPAN + 1000) * MAX_TABLE_SPAN


@pytest.mark.django_db
def test_export_begrenzt_spannweiten_im_altbestand(org: Any, autorin: Any) -> None:
    motion = _dokument(
        org, autorin, '<table><tbody><tr><td colspan="99999" rowspan="99999"><p>Zelle</p></td></tr></tbody></table>'
    )

    html = motion_export_service._render_html(motion)

    assert "<td><p>Zelle</p></td>" in html
    assert "99999" not in html
    assert motion_export_service.export_to_pdf(motion).startswith(b"%PDF")


@pytest.mark.django_db
def test_pdf_export_lehnt_zu_grosse_tabellen_ab(
    org: Any, autorin: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    motion = _dokument(org, autorin, _tabelle(110, 100, zelle=""))
    assert table_cell_count(_tabelle(110, 100, zelle="")) > MAX_PDF_TABLE_CELLS
    monkeypatch.setattr(
        motion_export_service, "_html_to_pdf", lambda html: pytest.fail("Die Umwandlung darf nicht beginnen")
    )
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    client = client_for(autorin.user)

    antwort = client.get(url, {"format": "pdf"})

    assert antwort.status_code == 400
    assert antwort.json() == {"error": PDF_TABLES_TOO_LARGE}
    with pytest.raises(ExportTooLargeError):
        motion_export_service.export_to_pdf(motion)
    # Die Word-Datei entsteht ohne Tabellenraster und bleibt möglich
    assert client.get(url, {"format": "docx"}).status_code == 200
    # Einreichung per E-Mail: feste Meldung statt eines Fehlers ohne Erklärung
    with pytest.raises(SubmissionError) as fehler:
        email_submission._read_attachments(motion)
    assert str(fehler.value) == PDF_TABLES_TOO_LARGE


@pytest.mark.django_db
def test_tabelle_unter_der_grenze_wird_exportiert(org: Any, autorin: Any) -> None:
    motion = _dokument(org, autorin, _tabelle(12, 4))

    assert motion_export_service.export_to_pdf(motion).startswith(b"%PDF")


# --- Bilder ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("daten", "art"),
    [
        (PNG, "png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01", "jpeg"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01", "jpg"),
        (b"GIF89a\x01\x00\x01\x00\x80\x00\x00", "gif"),
        (b"RIFF\x1a\x00\x00\x00WEBPVP8L", "webp"),
    ],
)
def test_eingebettete_rasterbilder_bleiben(daten: bytes, art: str) -> None:
    html = _bild(daten, art)

    assert sanitize_editor_html(html) == html


@pytest.mark.parametrize(
    "daten",
    [
        VEKTOR,
        b"\xef\xbb\xbf" + VEKTOR,
        b"  \n" + VEKTOR,
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03gepackt",
        b"<?xml version='1.0'?>" + VEKTOR,
        b"kein Bild, nur Text",
        b"\x89PNG",
    ],
)
@pytest.mark.parametrize("art", ["png", "jpeg", "gif", "webp"])
def test_bild_ohne_rastersignatur_faellt_weg(daten: bytes, art: str) -> None:
    assert sanitize_editor_html(f"<p>Text</p>{_bild(daten, art)}") == "<p>Text</p>"


def test_fuellzeichen_vor_den_bilddaten_gelten_nicht() -> None:
    """Die Signatur muss am Anfang der Daten stehen, wie jeder Leser sie dekodiert."""
    daten = base64.b64encode(PNG).decode("ascii")

    assert sanitize_editor_html(f'<img src="data:image/png;base64,===={daten}">') == ""
    assert sanitize_editor_html('<img src="data:image/png;base64,iVBORw0KGgo=">') == ""


@pytest.mark.django_db
def test_export_gibt_kein_vektorbild_an_die_umwandlung(org: Any, autorin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auch Altbestand: Ein Vektorbild unter der Bildart PNG steht direkt in der Datenbank."""
    from xhtml2pdf import xhtml2pdf_reportlab

    gelesen: list[bytes] = []
    echt = xhtml2pdf_reportlab.svg2rlg

    def mitschreiben(quelle: Any, **kwargs: Any) -> Any:
        gelesen.append(quelle.getvalue())
        return echt(quelle, **kwargs)

    monkeypatch.setattr(xhtml2pdf_reportlab, "svg2rlg", mitschreiben)
    motion = _dokument(org, autorin, f"<p>Vorher</p>{_bild(VEKTOR)}{_bild(PNG)}<p>Nachher</p>")

    html = motion_export_service._render_html(motion)
    pdf = motion_export_service.export_to_pdf(motion)

    assert base64.b64encode(VEKTOR).decode("ascii") not in html
    assert base64.b64encode(PNG).decode("ascii") in html
    assert pdf.startswith(b"%PDF") and gelesen == []


@pytest.mark.django_db
def test_docx_export_bereinigt_altbestand(org: Any, autorin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: list[str] = []
    echt = export_service.MotionExportService._html_to_docx

    def mitschreiben(self: Any, doc: Any, html: str) -> None:
        gesehen.append(html)
        echt(self, doc, html)

    monkeypatch.setattr(export_service.MotionExportService, "_html_to_docx", mitschreiben)
    motion = _dokument(org, autorin, f'<p onclick="x()">Text</p><script>weg()</script>{_bild(VEKTOR)}')

    assert motion_export_service.export_to_docx(motion).startswith(b"PK")
    assert gesehen == ["<p>Text</p>"]
