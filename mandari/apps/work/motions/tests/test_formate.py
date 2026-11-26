# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weitere Formate eines Dokuments (Issue #1584): Dokumentmodell, Word, OpenDocument, Markdown, Text und der Export.

Jede Datei wird zurückgelesen (Word mit python-docx, ODT als XML), damit Zeichenformate, Schriften, Listen, Tabellen,
Bilder, Links und Seitenumbrüche wirklich ankommen.
"""

from __future__ import annotations

import base64
import io
import zipfile
from typing import Any, cast
from xml.etree import ElementTree

import pytest
from django.urls import reverse

from apps.common import schriften
from apps.work.motions.formate import FORMATE, exportieren, modell
from apps.work.motions.formate.docx import docx_schreiben
from apps.work.motions.formate.odt import odt_schreiben
from apps.work.motions.formate.text import markdown_schreiben, text_schreiben
from apps.work.motions.models import Motion, OrganizationLetterhead
from apps.work.sanitize import sanitize_editor_html

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
BILD = f'<img src="data:image/png;base64,{base64.b64encode(PNG).decode("ascii")}" alt="Karte">'

HTML = sanitize_editor_html(
    "<h1>Antrag Tempo 30</h1>"
    '<p style="text-align: justify">Der Rat <strong>beschließt</strong>, <em>sofort</em> '
    '<span style="font-family: Tinos; font-size: 14pt">Tempo 30</span> und <s>nicht</s> '
    '<a href="https://example.org/plan">den Plan</a><br>umzusetzen.</p>'
    "<ul><li><p>Punkt eins</p><ul><li><p>Unterpunkt</p></li></ul></li><li><p>Punkt zwei</p></li></ul>"
    "<ol><li><p>Erstens</p></li><li><p>Zweitens</p></li></ol>"
    '<ul data-type="taskList"><li data-type="taskItem" data-checked="true"><label><input type="checkbox" checked>'
    "</label><div><p>Erledigt</p></div></li></ul>"
    "<blockquote><p>Ein Zitat</p></blockquote>"
    '<table class="editor-table"><tbody><tr><th colspan="2"><p>Kopf</p></th></tr>'
    "<tr><td><p>A</p></td><td><p>B</p></td></tr></tbody></table>"
    f"<hr>{BILD}"
    '<div data-page-break="true" class="page-break"></div><p>Nach dem Umbruch</p>'
)


def _bloecke() -> list[modell.Block]:
    return modell.lesen(HTML, 11)


def test_modell_liest_formate_listen_tabellen_und_bilder() -> None:
    bloecke = _bloecke()
    arten = [type(b).__name__ for b in bloecke]
    assert arten.count("Tabelle") == 1 and arten.count("Bild") == 1
    assert arten.count("Trennlinie") == 1 and arten.count("Seitenumbruch") == 1
    titel, absatz = bloecke[0], bloecke[1]
    assert isinstance(titel, modell.Absatz) and titel.art == "h1"
    assert isinstance(absatz, modell.Absatz) and absatz.ausrichtung == "justify"
    formate = {lauf.text: lauf.format for lauf in absatz.laeufe if not lauf.umbruch}
    assert formate["beschließt"].fett and formate["sofort"].kursiv
    assert formate["Tempo 30"].schrift == "tinos" and formate["Tempo 30"].groesse == 14
    assert formate["nicht"].durchgestrichen and formate["den Plan"].link == "https://example.org/plan"
    assert any(lauf.umbruch for lauf in absatz.laeufe)
    listen = [b for b in bloecke if isinstance(b, modell.Absatz) and b.liste]
    assert [(b.text, b.liste) for b in listen[:3]] == [
        ("Punkt eins", ("punkte", 0)),
        ("Unterpunkt", ("punkte", 1)),
        ("Punkt zwei", ("punkte", 0)),
    ]
    assert [(b.text, b.nummer) for b in listen if b.liste and b.liste[0] == "zahlen"] == [
        ("Erstens", 1),
        ("Zweitens", 2),
    ]
    aufgabe = next(b for b in listen if b.liste and b.liste[0] == "aufgaben")
    assert aufgabe.erledigt is True and aufgabe.text == "Erledigt"
    assert any(isinstance(b, modell.Absatz) and b.art == "zitat" for b in bloecke)
    tabelle = next(b for b in bloecke if isinstance(b, modell.Tabelle))
    assert tabelle.spaltenzahl == 2 and tabelle.zeilen[0][0].kopf and tabelle.zeilen[0][0].spalten == 2
    bild = next(b for b in bloecke if isinstance(b, modell.Bild))
    assert (bild.breite, bild.hoehe, bild.alt) == (1, 1, "Karte")


def _word(**kwargs: Any) -> Any:
    from docx import Document

    daten = docx_schreiben(
        _bloecke(),
        schrift=schriften.aufloesen("Calibri"),
        groesse=11,
        raender={"top": 30, "right": 25, "bottom": 25, "left": 25},
        **kwargs,
    )
    return Document(io.BytesIO(daten)), daten


def test_word_traegt_formate_schriften_und_struktur() -> None:
    doc, daten = _word()
    assert doc.styles["Normal"].font.name == "Calibri"
    assert doc.styles["Heading 1"].font.name == "Calibri"
    absatz = doc.paragraphs[1]
    laeufe = {run.text: run for run in absatz.runs}
    assert laeufe["beschließt"].bold and laeufe["sofort"].italic and laeufe["nicht"].font.strike
    assert laeufe["Tempo 30"].font.name == "Times New Roman"
    assert laeufe["Tempo 30"].font.size.pt == 14
    texte = [p.text for p in doc.paragraphs]
    assert "Punkt eins" in texte and "Unterpunkt" in texte and "☑ Erledigt" in texte
    assert doc.paragraphs[texte.index("Unterpunkt")].style.name == "List Bullet 2"
    assert doc.paragraphs[texte.index("Erstens")].style.name == "List Number"
    tabelle = doc.tables[0]
    assert tabelle.cell(0, 0).text == "Kopf" and tabelle.cell(0, 0)._tc is tabelle.cell(0, 1)._tc
    assert len(doc.inline_shapes) == 1
    xml = zipfile.ZipFile(io.BytesIO(daten)).read("word/document.xml").decode("utf-8")
    assert "<w:hyperlink" in xml and 'w:type="page"' in xml


def test_odt_ist_gueltiges_paket_mit_struktur() -> None:
    daten = odt_schreiben(
        _bloecke(),
        schrift=schriften.aufloesen("Times New Roman"),
        groesse=12,
        raender={"top": 30, "right": 25, "bottom": 25, "left": 25},
        titel="Antrag",
        fuss_zeilen=["Fraktion Muster"],
        seitenzahlen=True,
    )
    paket = zipfile.ZipFile(io.BytesIO(daten))
    erste = paket.infolist()[0]
    assert erste.filename == "mimetype" and erste.compress_type == zipfile.ZIP_STORED
    assert paket.read("mimetype") == b"application/vnd.oasis.opendocument.text"
    for name in ("content.xml", "styles.xml", "meta.xml", "META-INF/manifest.xml"):
        ElementTree.fromstring(paket.read(name))
    content = paket.read("content.xml").decode("utf-8")
    styles = paket.read("styles.xml").decode("utf-8")
    assert "<text:list " in content and "<table:table " in content and "draw:image" in content
    assert 'table:number-columns-spanned="2"' in content and "<table:covered-table-cell/>" in content
    assert 'xlink:href="https://example.org/plan"' in content and "<text:line-break/>" in content
    assert 'style:font-name="Times New Roman"' in content.replace("&quot;", '"') or "Times New Roman" in content
    assert 'fo:break-before="page"' in content
    assert "Times New Roman" in styles and "<text:page-number" in styles and "Fraktion Muster" in styles
    assert any(n.startswith("Pictures/") for n in paket.namelist())


def test_markdown_und_text() -> None:
    md = markdown_schreiben(_bloecke(), titel="Antrag")
    assert md.startswith("# Antrag Tempo 30\n")
    assert "**beschließt**" in md and "*sofort*" in md and "~~nicht~~" in md
    assert "[den Plan](https://example.org/plan)" in md
    assert "- Punkt eins\n  - Unterpunkt\n- Punkt zwei" in md
    assert "1. Erstens\n2. Zweitens" in md and "- [x] Erledigt" in md
    assert "| Kopf |  |\n| --- | --- |\n| A | B |" in md and "> Ein Zitat" in md
    txt = text_schreiben(_bloecke())
    assert "Antrag Tempo 30\n===============" in txt
    assert "• Punkt eins\n  • Unterpunkt" in txt and "[x] Erledigt" in txt and "A\tB" in txt
    assert "**" not in txt


# --- Export über die Ansicht --------------------------------------------------------------------------------------


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view"], email="formate@example.org")


def _dokument(org: Any, autor: Any, letterhead: OrganizationLetterhead | None = None) -> Motion:
    motion = Motion(organization=org, author=autor, title="Tempo 30", visibility="organization", letterhead=letterhead)
    cast(Any, motion).set_content_encrypted(HTML)
    motion.save()
    return motion


@pytest.mark.django_db
@pytest.mark.parametrize("schluessel", list(FORMATE))
def test_export_in_jedem_format(client: Any, org: Any, mitglied: Any, schluessel: str) -> None:
    motion = _dokument(org, mitglied)
    client.force_login(mitglied.user)
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    antwort = client.get(url, {"format": schluessel})
    assert antwort.status_code == 200
    assert antwort["Content-Type"] == FORMATE[schluessel].content_type
    assert antwort["Content-Disposition"].endswith(f'.{FORMATE[schluessel].endung}"')
    assert len(antwort.content) > 20


@pytest.mark.django_db
def test_unbekanntes_format(client: Any, org: Any, mitglied: Any) -> None:
    motion = _dokument(org, mitglied)
    client.force_login(mitglied.user)
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    assert client.get(url, {"format": "exe"}).status_code == 400


@pytest.mark.django_db
def test_word_nimmt_schrift_und_fusszeile_des_briefpapiers(org: Any, mitglied: Any) -> None:
    from docx import Document

    briefpapier = OrganizationLetterhead.objects.create(
        organization=org, name="B", kind="generated", font_family="Times New Roman", font_size=12, footer_text="Fuß"
    )
    doc = Document(io.BytesIO(exportieren(_dokument(org, mitglied, briefpapier), "docx")))
    assert doc.styles["Normal"].font.name == "Times New Roman"
    assert doc.styles["Normal"].font.size.pt == 12
    assert "Fuß" in "".join(p.text for p in doc.sections[0].footer.paragraphs)


@pytest.mark.django_db
def test_word_behaelt_frueheren_office_namen(org: Any, mitglied: Any) -> None:
    from docx import Document

    briefpapier = OrganizationLetterhead.objects.create(
        organization=org, name="G", kind="generated", font_family="Georgia"
    )
    doc = Document(io.BytesIO(exportieren(_dokument(org, mitglied, briefpapier), "docx")))
    assert doc.styles["Normal"].font.name == "Georgia"
