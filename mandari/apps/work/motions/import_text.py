# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Text und Gliederung importierter Dateien für den Editor (Issue #620).

**PDF.** pypdf liefert im einfachen Modus die Wörter zuverlässig, verliert aber je nach
Erzeuger die Zeilen: Manche Dateien kommen als eine einzige Zeile je Seite, andere mit einem
Zeilenumbruch nach jedem Wort. Der Layout-Modus bildet Zeilen und Absatzabstände nach, zerreißt
dafür bei Blocksatz und Zeichenabständen Wörter („Te m p o r e d u z i e r u n g“). Deshalb
kommen die Zeilen aus dem Layout-Modus und die Wortabstände aus dem einfachen Modus. Absätze
entstehen aus Leerzeilen, Aufzählungszeichen und kurzen Zeilen; getrennte Wörter am Zeilenende
werden wieder zusammengesetzt. Früher landete eine ganze Seite in einem einzigen Absatz.

**DOCX.** Absätze und Tabellen in Dokumentreihenfolge (Tabellen fielen früher ganz weg, etwa die
Unterschriften eines Antrags), Nummerierung als ``<ol>`` mit Ebenen, Aufzählungen als ``<ul>``,
weiche Zeilenumbrüche als ``<br>``, Links, Inhalte aus Steuerelementen und Überarbeitungen
(ohne gelöschten Text). „Listenabsatz“ ohne Nummerierung bleibt ein normaler Absatz.

Die Ausgabe geht anschließend durch ``apps.work.sanitize.sanitize_editor_html``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from html import escape, unescape
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# PDF
# =============================================================================

#: Zeilenanfänge, die einen neuen Absatz beginnen: 1. / 1) / 2.1 / a) / IV. / (3) / Aufzählungszeichen
_LIST_MARKER_RE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,2})*[.)]|\d{1,2}(?:\.\d{1,2})+|[a-hA-H][.)]|[IVX]{1,4}\.|\(\d{1,2}\)|\([a-h]\)"
    r"|[•▪◦●■‣∙·*\-–])\s"
)
#: Seitenzahlen in Kopf- oder Fußzeile („3“, „- 3 -“, „Seite 3 von 5“, „3/5“)
_PAGE_NUMBER_RE = re.compile(r"^[-–]?\s*(?:Seite\s+)?\d{1,3}(?:\s*(?:von|/)\s*\d{1,3})?\s*[-–]?$", re.IGNORECASE)
#: Wörter nach einem Ergänzungsstrich („Rats- und Gremienarbeit“) – dort bleibt der Strich stehen
_KEEP_HYPHEN_BEFORE = frozenset({"und", "oder", "bzw.", "bzw", "sowie", "bis", "als", "wie", "u.", "o.", "&"})
_ENDS_WITH_HYPHENATED_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]-$")

#: Kürzer als dieser Anteil einer vollen Zeile: Die Zeile beendet ihren Absatz (Überschrift,
#: Anschrift, letzte Zeile eines Absatzes).
SHORT_LINE_SHARE = 0.6
#: Mindestens dieser Anteil: Die letzte Zeile einer Seite läuft auf der nächsten weiter.
FULL_LINE_SHARE = 0.8


def _extract(page: Any, *, layout: bool) -> str:
    try:
        if layout:
            return str(page.extract_text(extraction_mode="layout") or "")
        return str(page.extract_text() or "")
    except Exception as exc:  # beschädigte Seiten, exotische Schriften
        logger.warning("PDF-Seite nicht lesbar (%s-Modus): %s", "Layout" if layout else "Text", type(exc).__name__)
        return ""


def merge_layout_lines(plain: str, layout: str) -> list[str]:
    """Zeilen aus dem Layout-Modus mit den Wortabständen des einfachen Modus.

    Für jede Layout-Zeile wird ihre Zeichenfolge (ohne Leerraum) im einfachen Text gesucht und
    mit dessen Abständen wiedergegeben. Findet sie sich dort nicht (andere Reihenfolge, etwa
    bei Wasserzeichen oder Spalten), bleibt die Layout-Zeile mit einfachen Leerzeichen stehen.
    Leere Layout-Zeilen bleiben leer – sie tragen die Absatzabstände.
    """
    chars: list[str] = []
    gap_before: list[bool] = []
    pending_gap = False
    for ch in plain:
        if ch.isspace():
            pending_gap = True
            continue
        chars.append(ch)
        gap_before.append(pending_gap)
        pending_gap = False
    stream = "".join(chars)

    lines: list[str] = []
    cursor = 0
    for raw in layout.splitlines():
        key = "".join(raw.split())
        if not key:
            lines.append("")
            continue
        pos = stream.find(key, cursor)
        if pos < 0:
            pos = stream.find(key)
        if pos < 0:
            lines.append(" ".join(raw.split()))
            continue
        end = pos + len(key)
        parts = [chars[pos]]
        for i in range(pos + 1, end):
            if gap_before[i]:
                parts.append(" ")
            parts.append(chars[i])
        lines.append("".join(parts))
        cursor = end
    return lines


def pdf_page_lines(data: bytes) -> tuple[list[list[str]], int] | None:
    """Zeilen je Seite (Leerzeilen = Abstand) und Seitenzahl; ``None``, wenn pypdf die Datei nicht öffnet."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:
        logger.warning("PDF nicht lesbar: %s", type(exc).__name__)
        return None

    result: list[list[str]] = []
    for page in pages:
        plain = _extract(page, layout=False)
        if not plain.strip():
            result.append([])
            continue
        layout = _extract(page, layout=True)
        result.append(merge_layout_lines(plain, layout) if layout.strip() else plain.splitlines())
    return result, len(pages)


def _without_page_numbers(lines: list[str]) -> list[str]:
    """Entfernt eine reine Seitenzahl als erste oder letzte Textzeile der Seite."""
    content = [i for i, line in enumerate(lines) if line.strip()]
    drop = {i for i in (content[:1] + content[-1:]) if _PAGE_NUMBER_RE.match(lines[i].strip())}
    return [line for i, line in enumerate(lines) if i not in drop]


def _join_lines(lines: list[str]) -> str:
    """Setzt umbrochene Zeilen zu einem Absatz zusammen (inkl. Silbentrennung)."""
    text = lines[0]
    for nxt in lines[1:]:
        if _ENDS_WITH_HYPHENATED_WORD_RE.search(text):
            first_word = nxt.split(" ", 1)[0]
            if nxt[:1].islower() and first_word not in _KEEP_HYPHEN_BEFORE:
                text = text[:-1] + nxt  # getrenntes Wort: gynäko-/logische
            elif nxt[:1].isupper() or nxt[:1].isdigit():
                text += nxt  # Bindestrich-Wort: Ad-hoc-/Maßnahmen, E-/Mail
            else:
                text += " " + nxt  # Ergänzungsstrich: Rats-/und Gremienarbeit
        else:
            text += " " + nxt
    return " ".join(text.split())


def paragraphs_from_lines(pages: list[list[str]], blank_separates: bool = False) -> list[str]:
    """Absätze aus Zeilen je Seite.

    Ein neuer Absatz beginnt nach einem größeren Abstand als dem üblichen Zeilenabstand, bei
    einem Aufzählungszeichen, nach einer Zeile mit Doppelpunkt und nach einer kurzen Zeile.
    Ein Absatz läuft über die Seitengrenze, wenn die letzte Zeile voll ist und die nächste
    klein beginnt.
    """
    entries_per_page: list[list[tuple[str, int]]] = []
    gap_counts: Counter[int] = Counter()
    lengths: list[int] = []
    for lines in pages:
        entries: list[tuple[str, int]] = []
        blank = 0
        for line in _without_page_numbers(lines):
            text = " ".join(line.split())
            if not text:
                blank += 1
                continue
            if entries:
                gap_counts[blank] += 1
            entries.append((text, blank))
            lengths.append(len(text))
            blank = 0
        entries_per_page.append(entries)
    if not lengths:
        return []

    lengths.sort()
    full = lengths[min(len(lengths) - 1, int(len(lengths) * 0.9))]
    # Üblicher Zeilenabstand: der kleinste Abstand, der oft genug vorkommt. Manche Dateien setzen im
    # Layout-Modus zwischen jede Zeile eine Leerzeile – dann trennt erst ein größerer Abstand Absätze.
    # Bei wenigen Zeilen oder Texterkennung (``blank_separates``) trennt jede Leerzeile.
    transitions = sum(gap_counts.values())
    normal_gap = 0
    if not blank_separates and transitions >= 5:
        normal_gap = min((gap for gap, n in gap_counts.items() if n >= 0.2 * transitions), default=0)

    paragraphs: list[str] = []
    current: list[str] = []
    previous: str | None = None
    for entries in entries_per_page:
        for index, (text, gap) in enumerate(entries):
            if previous is None:
                starts_new = True
            elif index == 0:
                starts_new = not (len(previous) >= FULL_LINE_SHARE * full and text[:1].islower())
            else:
                starts_new = (
                    gap > normal_gap
                    or bool(_LIST_MARKER_RE.match(text))
                    or previous.endswith(":")
                    or len(previous) < SHORT_LINE_SHARE * full
                )
            if starts_new and current:
                paragraphs.append(_join_lines(current))
                current = []
            current.append(text)
            previous = text
    if current:
        paragraphs.append(_join_lines(current))
    return paragraphs


def text_paragraphs(text: str) -> list[str]:
    """Absätze aus reinem Text (z. B. Texterkennung): Leerzeilen trennen Seiten und Absätze."""
    return paragraphs_from_lines([text.replace("\r\n", "\n").split("\n")], blank_separates=True)


#: Typografische Ligaturen aus PDF-Schriften (ﬁ, ﬂ …) als einzelne Buchstaben – für Suche und Bearbeitung
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬆ": "st"})


def paragraphs_to_html(paragraphs: list[str]) -> str:
    return "\n".join(f"<p>{escape(p.translate(_LIGATURES), quote=False)}</p>" for p in paragraphs if p.strip())


# =============================================================================
# DOCX
# =============================================================================

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


#: Inline-Container, deren Läufe zum Absatz gehören (Überarbeitung „eingefügt“, Felder, Steuerelemente …)
_INLINE_CONTAINERS = frozenset(
    _w(t) for t in ("ins", "smartTag", "fldSimple", "customXml", "moveTo", "sdt", "sdtContent", "dir", "bdo")
)
_OL_TYPES = {"lowerLetter": "a", "upperLetter": "A", "lowerRoman": "i", "upperRoman": "I"}
_ALIGNMENTS = {"center": "center", "right": "right", "end": "right"}


@dataclass
class _ListItem:
    level: int
    kind: str  # "ol" oder "ul"
    ol_type: str | None
    start: int
    html: str


def _numbering_formats(document: Any) -> dict[tuple[str, str], str]:
    """(numId, Ebene) → Zahlenformat (decimal, bullet, lowerLetter …) aus numbering.xml."""
    try:
        numbering = document.part.numbering_part.element
    except Exception:
        return {}
    abstract: dict[str, dict[str, str]] = {}
    for definition in numbering.findall(_w("abstractNum")):
        levels: dict[str, str] = {}
        for lvl in definition.findall(_w("lvl")):
            fmt = lvl.find(_w("numFmt"))
            levels[lvl.get(_w("ilvl"), "0")] = fmt.get(_w("val"), "decimal") if fmt is not None else "decimal"
        abstract[definition.get(_w("abstractNumId"), "")] = levels
    formats: dict[tuple[str, str], str] = {}
    for num in numbering.findall(_w("num")):
        ref = num.find(_w("abstractNumId"))
        if ref is None:
            continue
        for ilvl, fmt in abstract.get(ref.get(_w("val"), ""), {}).items():
            formats[(num.get(_w("numId"), ""), ilvl)] = fmt
    return formats


def _num_pr(paragraph: Any) -> tuple[str, str] | None:
    """numId und Ebene aus dem Absatz oder seiner Formatvorlage (inkl. Basisvorlagen)."""
    candidates = [paragraph._p.pPr]
    try:
        style = paragraph.style
        for _ in range(10):  # Basisvorlagen-Kette, gegen Zyklen begrenzt
            if style is None:
                break
            candidates.append(style.element.pPr)
            style = style.base_style
    except Exception:
        pass
    for ppr in candidates:
        if ppr is None or ppr.numPr is None:
            continue
        num_id = ppr.numPr.numId
        if num_id is None:
            continue
        ilvl = ppr.numPr.ilvl
        return str(num_id.val), str(ilvl.val if ilvl is not None else 0)
    return None


def _style_name(paragraph: Any) -> str:
    try:
        return (paragraph.style.name or "").lower() if paragraph.style is not None else ""
    except Exception:
        return ""


def _hyperlink_target(element: Any, paragraph: Any) -> str | None:
    rel_id = element.get(f"{{{_R_NS}}}id")
    if not rel_id:
        return None
    try:
        rel = paragraph.part.rels[rel_id]
    except KeyError:
        return None
    target = str(rel.target_ref) if rel.is_external else ""
    return target if re.match(r"^(?:https?:|mailto:)", target, re.IGNORECASE) else None


def _run_html(run: Any) -> str:
    text = run.text or ""
    if not text:
        return ""
    html = escape(text.replace("\t", " "), quote=False).replace("\n", "<br>")
    if run.bold:
        html = f"<strong>{html}</strong>"
    if run.italic:
        html = f"<em>{html}</em>"
    if run.underline:
        html = f"<u>{html}</u>"
    if run.font is not None and run.font.strike:
        html = f"<s>{html}</s>"
    return html


def _inline_html(container: Any, paragraph: Any) -> str:
    from docx.text.run import Run

    parts: list[str] = []
    for child in container.iterchildren():
        tag = child.tag
        if tag == _w("r"):
            parts.append(_run_html(Run(child, paragraph)))
        elif tag == _w("hyperlink"):
            inner = _inline_html(child, paragraph)
            href = _hyperlink_target(child, paragraph)
            parts.append(f'<a href="{escape(href)}">{inner}</a>' if href and inner else inner)
        elif tag in _INLINE_CONTAINERS:
            parts.append(_inline_html(child, paragraph))
        # w:del, w:moveFrom (gelöschter Text), w:pPr, Kommentarmarken: entfallen
    return "".join(parts)


_ADJACENT_MARKS_RE = re.compile(r"</(strong|em|u|s)><\1>")
_EDGE_BREAKS_RE = re.compile(r"^(?:\s|<br>)+|(?:\s|<br>)+$")


def _paragraph_inline(paragraph: Any) -> str:
    html = _inline_html(paragraph._p, paragraph)
    # Nur Leerraum/Umbrüche: leerer Absatz
    if not re.sub(r"<br>|<[^>]+>|\s", "", html):
        return ""
    # Word teilt formatierten Text in viele Läufe: <strong>a</strong><strong>b</strong> → <strong>ab</strong>
    html = _ADJACENT_MARKS_RE.sub("", html)
    return _EDGE_BREAKS_RE.sub("", html)


def _alignment(paragraph: Any) -> str | None:
    ppr = paragraph._p.pPr
    jc = ppr.find(_w("jc")) if ppr is not None else None
    return _ALIGNMENTS.get(jc.get(_w("val"), "")) if jc is not None else None


def _heading_tag(style_name: str) -> str | None:
    if style_name in ("title", "titel") or style_name.startswith(("heading 1", "überschrift 1")):
        return "h1"
    if style_name.startswith(("heading 2", "überschrift 2")):
        return "h2"
    if style_name.startswith(("heading 3", "überschrift 3")):
        return "h3"
    return None


def _render_lists(items: list[_ListItem]) -> str:
    """Verschachtelte Listen aus flachen Einträgen mit Ebene."""
    out: list[str] = []

    def render(start: int, level: int) -> int:
        first = items[start]
        attrs = ""
        if first.kind == "ol":
            if first.ol_type:
                attrs += f' type="{first.ol_type}"'
            if first.start > 1:
                attrs += f' start="{first.start}"'
        out.append(f"<{first.kind}{attrs}>")
        i = start
        while i < len(items) and items[i].level >= level:
            item = items[i]
            if item.level > level:
                # Tiefere Ebene ohne übergeordneten Eintrag: eigene Liste
                i = render(i, item.level)
                continue
            if item.kind != first.kind:
                break
            out.append(f"<li><p>{item.html}</p>")
            i += 1
            while i < len(items) and items[i].level > level:
                i = render(i, items[i].level)
            out.append("</li>")
        out.append(f"</{first.kind}>")
        return i

    index = 0
    while index < len(items):
        index = render(index, items[index].level)
    return "".join(out)


def _table_html(table: Any) -> str:
    rows: list[str] = []
    has_text = False
    for row in table.rows:
        cells_html: list[str] = []
        seen: list[Any] = []
        row_has_text = False
        for cell in row.cells:
            # Verbundene Zellen liefert python-docx je Rasterspalte erneut – nur einmal ausgeben
            if any(cell._tc is known for known in seen):
                continue
            seen.append(cell._tc)
            inner = "".join(f"<p>{html}</p>" for html in (_paragraph_inline(p) for p in cell.paragraphs) if html)
            row_has_text = row_has_text or bool(inner)
            cells_html.append(f"<td>{inner or '<p></p>'}</td>")
        if row_has_text:  # leere Abstandszeilen entfallen
            has_text = True
            rows.append("<tr>" + "".join(cells_html) + "</tr>")
    if not has_text:
        return ""  # leere Layout-Tabellen (z. B. Briefkopf-Raster) entfallen
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


def _iter_blocks(parent: Any, container: Any) -> Any:
    """Absätze und Tabellen in Dokumentreihenfolge, auch innerhalb von Inhaltssteuerelementen."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in parent.iterchildren():
        if child.tag == _w("p"):
            yield Paragraph(child, container)
        elif child.tag == _w("tbl"):
            yield Table(child, container)
        elif child.tag == _w("sdt"):
            content = child.find(_w("sdtContent"))
            if content is not None:
                yield from _iter_blocks(content, container)


def docx_to_html(data: bytes) -> str:
    """Editor-HTML aus einer DOCX-Datei (vor der Bereinigung durch die Positivliste)."""
    from docx import Document
    from docx.table import Table

    document = Document(BytesIO(data))
    formats = _numbering_formats(document)
    body = document.element.body
    container = document._body

    parts: list[str] = []
    pending: list[_ListItem] = []
    counters: dict[tuple[str, int], int] = {}

    def flush() -> None:
        if pending:
            parts.append(_render_lists(pending))
            pending.clear()

    for block in _iter_blocks(body, container):
        if isinstance(block, Table):
            flush()
            try:
                parts.append(_table_html(block))
            except Exception as exc:  # unregelmäßige Tabellen: Text der Zellen retten
                logger.warning("DOCX-Tabelle nicht lesbar: %s", type(exc).__name__)
                text = " ".join(t for t in (c.strip() for c in block._tbl.itertext()) if t)
                if text:
                    parts.append(f"<p>{escape(text, quote=False)}</p>")
            continue

        inline = _paragraph_inline(block)
        if not inline:
            continue
        style_name = _style_name(block)
        heading = _heading_tag(style_name)
        if heading:
            # Überschriften mit Gliederungsnummer bleiben Überschriften
            flush()
            parts.append(f"<{heading}>{inline}</{heading}>")
            continue
        num = _num_pr(block)
        fmt = formats.get(num, "decimal") if num else None
        if num and num[0] != "0" and fmt != "none":
            level = int(num[1]) if num[1].isdigit() else 0
            key = (num[0], level)
            counters[key] = counters.get(key, 0) + 1
            # Eine höhere Ebene beginnt die tieferen neu (Word-Standard)
            for other in [k for k in counters if k[0] == num[0] and k[1] > level]:
                del counters[other]
            kind = "ul" if fmt == "bullet" else "ol"
            pending.append(
                _ListItem(level=level, kind=kind, ol_type=_OL_TYPES.get(fmt or ""), start=counters[key], html=inline)
            )
            continue
        if style_name.startswith("list bullet"):
            pending.append(_ListItem(level=0, kind="ul", ol_type=None, start=1, html=inline))
            continue

        flush()
        if style_name == "quote" or style_name.startswith(("block", "intense quote")):
            parts.append(f"<blockquote><p>{inline}</p></blockquote>")
            continue
        align = _alignment(block)
        style = f' style="text-align: {align}"' if align else ""
        parts.append(f"<p{style}>{inline}</p>")
    flush()
    return "\n".join(p for p in parts if p)


def html_to_text(html: str) -> str:
    """Suchtext aus Editor-HTML: Blöcke als Zeilen, Tags entfernt."""
    text = re.sub(r"<br\s*/?>|</(?:p|h[1-6]|li|td|tr|blockquote)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())
