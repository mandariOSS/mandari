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


@pytest.mark.django_db
def test_dateiname_mit_umlaut(client: Any, org: Any, mitglied: Any) -> None:
    # Umlaute als filename* (RFC 6266): Ein roher Umlaut im Header lässt Chromium den Namen verwerfen
    motion = _dokument(org, mitglied)
    motion.title = "Prüfen"
    motion.save()
    client.force_login(mitglied.user)
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    for schluessel in ("odt", "pdf"):
        antwort = client.get(url, {"format": schluessel})
        assert antwort["Content-Disposition"] == f"attachment; filename*=utf-8''Pr%C3%BCfen.{schluessel}"


# --- Befunde der Prüfung von #1587 -----------------------------------------------------------------------------------

RAENDER: dict[str, float] = {"top": 30, "right": 25, "bottom": 25, "left": 25}


def _lesen(html: str) -> list[modell.Block]:
    return modell.lesen(sanitize_editor_html(html), 11)


def _docx(html: str) -> tuple[Any, str]:
    from docx import Document

    daten = docx_schreiben(_lesen(html), schrift=schriften.aufloesen(None), groesse=11, raender=RAENDER)
    return Document(io.BytesIO(daten)), zipfile.ZipFile(io.BytesIO(daten)).read("word/document.xml").decode("utf-8")


def _odt(html: str) -> str:
    daten = odt_schreiben(_lesen(html), schrift=schriften.aufloesen(None), groesse=11, raender=RAENDER)
    content = zipfile.ZipFile(io.BytesIO(daten)).read("content.xml")
    ElementTree.fromstring(content)
    return content.decode("utf-8")


ZEILENSPANNE = (
    '<table><tbody><tr><td rowspan="2"><p>A</p></td><td><p>B</p></td></tr><tr><td><p>C</p></td></tr></tbody></table>'
)


def test_raster_beachtet_zeilenspannen() -> None:
    (tabelle,) = _lesen(
        '<table><tbody><tr><td rowspan="2"><p>A</p></td></tr><tr><td><p>B</p></td></tr></tbody></table>'
    )
    assert isinstance(tabelle, modell.Tabelle)
    spalten, plaetze = tabelle.raster()
    assert spalten == 2 and plaetze[1][0].spalte == 1
    doc, _xml = _docx('<table><tbody><tr><td rowspan="2"><p>A</p></td></tr><tr><td><p>B</p></td></tr></tbody></table>')
    assert doc.tables[0].cell(1, 1).text == "B"
    assert ">B<" in _odt(
        '<table><tbody><tr><td rowspan="2"><p>A</p></td></tr><tr><td><p>B</p></td></tr></tbody></table>'
    )


def test_ueberdeckte_felder_bleiben_in_markdown_und_text_leer() -> None:
    md = markdown_schreiben(_lesen(ZEILENSPANNE))
    assert "| A | B |\n| --- | --- |\n|  | C |" in md
    assert "A\tB\n\tC" in text_schreiben(_lesen(ZEILENSPANNE))


def test_bild_in_tabellenzelle_in_jedem_format() -> None:
    html = f"<table><tbody><tr><td><p>Lage</p>{BILD}</td><td><p>B</p></td></tr></tbody></table>"
    (tabelle,) = _lesen(html)
    assert isinstance(tabelle, modell.Tabelle)
    assert [type(b).__name__ for b in tabelle.zeilen[0][0].bloecke] == ["Absatz", "Bild"]
    doc, _xml = _docx(html)
    assert len(doc.inline_shapes) == 1 and doc.tables[0].cell(0, 1).text == "B"
    assert "draw:image" in _odt(html)
    assert "| Lage *[Bild: Karte]* | B |" in markdown_schreiben(_lesen(html))
    assert "Lage [Bild: Karte]\tB" in text_schreiben(_lesen(html))


def test_webp_wird_als_png_eingebettet() -> None:
    from PIL import Image

    puffer = io.BytesIO()
    Image.new("RGB", (4, 2), "red").save(puffer, format="WEBP")
    webp = base64.b64encode(puffer.getvalue()).decode("ascii")
    (bild,) = _lesen(f'<img src="data:image/webp;base64,{webp}" alt="Plan">')
    assert isinstance(bild, modell.Bild)
    assert (bild.art, bild.breite, bild.hoehe, bild.alt) == ("png", 4, 2, "Plan") and bild.daten.startswith(b"\x89PNG")


def test_listen_mit_startwert_und_eigener_nummerierung() -> None:
    html = '<ol start="5"><li><p>Fünf</p></li><li><p>Sechs</p></li></ol><p>Text</p><ol><li><p>Eins</p></li></ol>'
    doc, _xml = _docx(html)
    nummern = {p.text: p._p.pPr.numPr.numId.val for p in doc.paragraphs if p._p.pPr is not None and p._p.pPr.numPr}
    # Jede Liste zählt für sich: die zweite beginnt wieder bei 1, die erste bei 5
    assert nummern["Fünf"] == nummern["Sechs"] != nummern["Eins"]
    numbering = doc.part.numbering_part.element.xml
    assert 'w:startOverride w:val="5"' in numbering and 'w:startOverride w:val="1"' in numbering
    content = _odt(html)
    assert 'text:start-value="5"' in content and content.count('text:style-name="LZahlen"') == 2


def test_odt_trennt_listen_verschiedener_art() -> None:
    content = _odt("<ol><li><p>Erstens</p></li></ol><ul><li><p>Punkt</p></li></ul>")
    zahlen = content.index('<text:list text:style-name="LZahlen">')
    punkte = content.index('<text:list text:style-name="LPunkte">')
    assert zahlen < content.index(">Erstens<") < punkte < content.index(">Punkt<")
    # Unterliste anderer Art und ein weiterer Absatz nach ihr bleiben im selben Punkt
    content = _odt("<ol><li><p>A</p><ul><li><p>a</p></li></ul><p>A2</p></li><li><p>B</p></li></ol>")
    assert content.count("<text:list-item>") == 3 and content.index("A2") < content.index(">B<")


def test_odt_codeblock_umbruch_und_seitenumbruch_vor_tabelle() -> None:
    content = _odt(
        "<pre><code>zeile 1\nzeile 2</code></pre>"
        '<div data-page-break="true" class="page-break"></div>'
        "<table><tbody><tr><td><p>X</p></td></tr></tbody></table>"
    )
    assert "zeile 1<text:line-break/>zeile 2" in content
    tabelle = content.split('<style:style style:name="Tabelle1" style:family="table">', 1)[1].split("</style:style>")[0]
    assert 'fo:break-before="page"' in tabelle


def test_markdown_schuetzt_linkziele_und_listenmarken() -> None:
    md = markdown_schreiben(
        _lesen('<p><a href="https://example.org/a)b">Plan</a></p><p>1. Januar</p><p>- kein Punkt</p><p>---</p>')
    )
    assert "[Plan](<https://example.org/a)b>)" in md
    assert r"1\. Januar" in md and r"\- kein Punkt" in md and r"\---" in md


@pytest.mark.django_db
def test_aufgabenliste_ueber_den_export(org: Any, mitglied: Any) -> None:
    md = exportieren(_dokument(org, mitglied), "md").decode("utf-8")
    assert "- [x] Erledigt" in md


@pytest.mark.django_db
def test_zu_grosse_tabelle_wird_abgelehnt(client: Any, org: Any, mitglied: Any) -> None:
    motion = _dokument(org, mitglied)
    zeilen = "".join("<tr><td><p>x</p></td></tr>" for _ in range(499))
    cast(Any, motion).set_content_encrypted(
        "<table><tbody><tr>" + "<td><p>y</p></td>" * 500 + f"</tr>{zeilen}</tbody></table>"
    )
    motion.save()
    client.force_login(mitglied.user)
    url = reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    for schluessel in ("docx", "odt", "md", "txt"):
        antwort = client.get(url, {"format": schluessel})
        assert antwort.status_code == 400
        assert "zu groß" in antwort.json()["error"]


def test_word_setzt_zellbreiten_wie_das_raster() -> None:
    doc, _xml = _docx(
        '<table><colgroup><col style="width: 100px"><col style="width: 300px"></colgroup>'
        "<tbody><tr><td><p>A</p></td><td><p>B</p></td></tr></tbody></table>"
    )
    links, rechts = doc.tables[0].rows[0].cells
    assert abs(rechts.width / links.width - 3) < 0.01


def test_startwert_null_alternativtext_und_unterlisten_verschiedener_art() -> None:
    html = '<ol start="0"><li><p>Null</p></li><li><p>Eins</p></li></ol>'
    content = _odt(html)
    assert 'text:start-value="0"' in content
    assert "0. Null\n1. Eins" in markdown_schreiben(_lesen(html))
    assert 'w:startOverride w:val="0"' in _docx(html)[0].part.numbering_part.element.xml
    # Alternativtext des Bilds in Word
    _doc, xml = _docx(BILD)
    assert 'descr="Karte"' in xml
    # Aufzählung und Nummerierung direkt nacheinander im selben Punkt: zwei Unterlisten
    content = _odt("<ol><li><p>A</p><ul><li><p>a</p></li></ul><ol><li><p>b</p></li></ol></li></ol>")
    unter = content.split(">A<", 1)[1]
    assert unter.index('text:style-name="LPunkte"') < unter.index(">a<") < unter.index('text:style-name="LZahlen"')
    assert unter.index('text:style-name="LZahlen"') < unter.index(">b<")
