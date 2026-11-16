# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Detailseiten von ALLRIS 4 (CC e-gov, Wicket-Oberfläche unter ``/public/``) lesen, Issue #1431.

ALLRIS 4 bietet oft eine OParl-Schnittstelle, die die Struktur vollständig liefert, die Inhalte aber nicht:
Beschlusstext, Abstimmungsergebnis und Wortprotokoll eines Tagesordnungspunkts, Beschlussvorschlag und Sachverhalt
einer Vorlage fehlen dort (Stand Herzogenrath 11/2026). Die Detailseiten, auf die das OParl-Objekt mit ``web``
verweist, sind serverseitig gerendert und unter fester Adresse per GET abrufbar:

- ``to020?TOLFDNR=N``  Tagesordnungspunkt: Abschnitte „Wortprotokoll“, „Beschluss“, „Abstimmungsergebnis“
- ``vo020?VOLFDNR=N``  Vorlage: Abschnitte „Beschlussvorschlag“, „Sachverhalt“ (auch „Begründung“)

Jeder Abschnitt ist ein Kasten ``div.compFull`` mit Überschrift ``h2.expandedTitle``; der Text steht in
``div.docPart`` (aus Word erzeugtes HTML). Die Funktionen hier lesen nur diese Abschnitte und liefern die Felder
so, wie die Ergänzung sie in das OParl-Objekt schreibt (``src/sync/html_ergaenzung.py``): ``resolutionText``,
``mandari:vote`` und ``mandari:minutes`` am Tagesordnungspunkt, ``mandari:proposal`` und ``mandari:explanation`` an
der Vorlage – dieselben Schlüssel wie beim ALLRIS-3-Adapter (``src/scrapers/allris3.py``).

Anlagen verlinkt ALLRIS 4 nur über sitzungsgebundene Wicket-Adressen (``…attachment-link``); sie sind ohne Sitzung
nicht abrufbar und bleiben außen vor. Ihr Inhalt steckt im Sammeldokument, das OParl als ``mainFile`` liefert.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

#: Ergebnis der Beschlussfassung (``result`` aus OParl, Text von ALLRIS) -> Code von ``mandari:vote.result``
_RESULT_CODES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"abgelehnt", re.IGNORECASE), "rejected"),
    (re.compile(r"zurückgezogen", re.IGNORECASE), "withdrawn"),
    (re.compile(r"vertagt|zurückgestellt|verwiesen|abgesetzt", re.IGNORECASE), "deferred"),
    (re.compile(r"kenntnis", re.IGNORECASE), "noted"),
    (re.compile(r"beschlossen|zugestimmt|angenommen|empfohlen", re.IGNORECASE), "approved"),
)
_YES_RE = re.compile(r"Ja\s*-?\s*Stimmen\s*:?\s*(\d+|einstimmig)", re.IGNORECASE)
_NO_RE = re.compile(r"Nein\s*-?\s*Stimmen\s*:?\s*(\d+)", re.IGNORECASE)
_ABSTAIN_RE = re.compile(r"Enthaltung(?:en)?\s*:?\s*(\d+)", re.IGNORECASE)
_UNANIMOUS_RE = re.compile(r"\beinstimmig\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t    ]+")
#: Blöcke, die im Text eine eigene Zeile bilden
_BLOCKS = ("p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div")

#: Abschnittsüberschriften (klein, ohne Satzzeichen am Ende) -> Feld
AGENDA_ITEM_SECTIONS = {
    "wortprotokoll": "minutes",
    "protokoll": "minutes",
    "beschluss": "resolution",
    "abstimmungsergebnis": "vote",
}
PAPER_SECTIONS = {
    "beschlussvorschlag": "proposal",
    "sachverhalt": "explanation",
    "begründung": "explanation",
    "sachverhalt und begründung": "explanation",
}


def is_allris4_page(soup: BeautifulSoup) -> bool:
    """Ist das eine Detailseite von ALLRIS 4 (Rahmen ``#allris`` mit Seitentitel ``h1.title`` im ``#pageContent``)?"""
    frame = soup.find(id="allris")
    content = frame.find(id="pageContent") if isinstance(frame, Tag) else None
    return isinstance(content, Tag) and content.find("h1", class_="title") is not None


def basic_data(soup: BeautifulSoup) -> dict[str, str]:
    """Grunddaten der Seite (``dl`` mit ``dt.colTitle``): Bezeichnung (klein, ohne Doppelpunkt) -> Text."""
    data: dict[str, str] = {}
    for title in soup.find_all("dt", class_="colTitle"):
        label = title.get_text(" ", strip=True).rstrip(":").strip().lower()
        value = title.find_next_sibling("dd")
        if label and isinstance(value, Tag):
            text = _SPACE_RE.sub(" ", value.get_text(" ", strip=True)).strip()
            if text:
                data.setdefault(label, text)
    return data


def _block_lines(node: Tag) -> list[str]:
    """Text eines Abschnitts als Zeilen: ein Block (Absatz, Listenpunkt, Tabellenzeile) je Zeile, ohne Leerzeilen."""
    lines: list[str] = []
    for element in node.find_all(_BLOCKS):
        if element.find(_BLOCKS) is not None:
            continue  # nur innerste Blöcke, sonst stünde der Text doppelt da
        if element.name == "tr":
            cells = [_SPACE_RE.sub(" ", c.get_text(" ", strip=True)).strip() for c in element.find_all(["td", "th"])]
            text = " | ".join(cell for cell in cells if cell)
        else:
            text = _SPACE_RE.sub(" ", element.get_text(" ", strip=True)).strip()
        if text:
            lines.append(text)
    if not lines:
        text = _SPACE_RE.sub(" ", node.get_text(" ", strip=True)).strip()
        if text:
            lines.append(text)
    return lines


def _sections(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Abschnitte der Seite: Überschrift (klein) -> Zeilen der Textteile (``div.docPart``) darunter."""
    found: dict[str, list[str]] = {}
    for heading in soup.find_all("h2", class_="expandedTitle"):
        title = heading.get_text(" ", strip=True).rstrip(":").strip().lower()
        box = heading.parent
        if not isinstance(box, Tag) or not title:
            continue
        parts = box.find_all("div", class_="docPart")
        lines: list[str] = []
        for part in parts:
            lines.extend(_block_lines(part))
        if lines:
            found.setdefault(title, []).extend(lines)
    return found


def _drop_label(lines: list[str], label: str) -> list[str]:
    """Erste Zeile ohne die Wiederholung der Überschrift („Beschluss:“ als eigene Zeile oder als Anfang)."""
    if not lines:
        return lines
    first = lines[0]
    pattern = re.compile(rf"^{re.escape(label)}\s*:?\s*", re.IGNORECASE)
    if pattern.fullmatch(first):
        return lines[1:]
    stripped = pattern.sub("", first, count=1)
    if stripped != first:
        return [stripped, *lines[1:]] if stripped else lines[1:]
    return lines


def _text(lines: list[str]) -> str | None:
    text = "\n".join(line for line in lines if line).strip()
    return text or None


def result_code(result: Any) -> str | None:
    """Code der Beschlussfassung (``approved``, ``rejected`` …) aus dem Text von ALLRIS; ``None``, wenn unklar."""
    if not isinstance(result, str) or not result.strip():
        return None
    for pattern, code in _RESULT_CODES:
        if pattern.search(result):
            return code
    return None


def parse_vote(lines: list[str], result: Any = None) -> dict[str, Any] | None:
    """
    Abstimmungsergebnis als ``mandari:vote`` (nur Summen): ``{"method": "summary", "yes", "no", "abstain"}`` und,
    wenn aus ``result`` ablesbar, ``result``. „Einstimmig“ statt einer Zahl lässt ``yes`` offen (die Zahl der
    Anwesenden nennt die Seite nicht). ``None``, wenn die Seite keine Summe nennt.
    """
    text = " ".join(lines)
    yes_match, no_match, abstain_match = _YES_RE.search(text), _NO_RE.search(text), _ABSTAIN_RE.search(text)
    unanimous = _UNANIMOUS_RE.search(text) is not None
    if not (yes_match or no_match or abstain_match or unanimous):
        return None
    vote: dict[str, Any] = {"method": "summary"}
    if yes_match and yes_match.group(1).isdigit():
        vote["yes"] = int(yes_match.group(1))
    if no_match:
        vote["no"] = int(no_match.group(1))
    elif unanimous:
        vote["no"] = 0
    if abstain_match:
        vote["abstain"] = int(abstain_match.group(1))
    elif unanimous:
        vote["abstain"] = 0
    code = result_code(result)
    if code is not None:
        vote["result"] = code
    return vote


def parse_agenda_item_page(html: str, result: Any = None) -> dict[str, Any] | None:
    """
    Felder aus der Seite eines Tagesordnungspunkts (``to020``): ``resolutionText`` (Beschluss, darunter das
    Abstimmungsergebnis als Text), ``mandari:vote`` und ``mandari:minutes``. ``result`` ist die Beschlussart aus
    OParl (für ``mandari:vote.result``). Fehlende Abschnitte fehlen im Ergebnis; ``None``, wenn die Seite keine
    Detailseite von ALLRIS 4 ist.
    """
    soup = BeautifulSoup(html, "html.parser")
    if not is_allris4_page(soup):
        return None
    if not (isinstance(result, str) and result.strip()):
        # In Sitzungen eingebettete Punkte tragen bei ALLRIS 4 kein ``result``: dann die Beschlussart der Seite
        result = basic_data(soup).get("beschluss")
    sections = _sections(soup)
    fields: dict[str, Any] = {}
    by_field: dict[str, list[str]] = {}
    for title, lines in sections.items():
        field = AGENDA_ITEM_SECTIONS.get(title)
        if field is not None:
            by_field.setdefault(field, []).extend(lines)

    resolution = _drop_label(by_field.get("resolution", []), "Beschluss")
    vote_lines = _drop_label(by_field.get("vote", []), "Abstimmungsergebnis")
    resolution_parts = [part for part in (_text(resolution), _text(vote_lines)) if part]
    if resolution_parts:
        if vote_lines and _text(vote_lines):
            resolution_parts[-1] = f"Abstimmungsergebnis:\n{resolution_parts[-1]}"
        fields["resolutionText"] = "\n\n".join(resolution_parts)
    vote = parse_vote(vote_lines, result) if vote_lines else None
    if vote is not None:
        fields["mandari:vote"] = vote
    minutes = _text(_drop_label(by_field.get("minutes", []), "Wortprotokoll"))
    if minutes:
        fields["mandari:minutes"] = minutes
    return fields


def parse_paper_page(html: str) -> dict[str, Any] | None:
    """
    Felder aus der Seite einer Vorlage (``vo020``): ``mandari:proposal`` (Beschlussvorschlag) und
    ``mandari:explanation`` (Sachverhalt bzw. Begründung). ``None``, wenn die Seite keine Detailseite von ALLRIS 4 ist.
    """
    soup = BeautifulSoup(html, "html.parser")
    if not is_allris4_page(soup):
        return None
    by_field: dict[str, list[str]] = {}
    for title, lines in _sections(soup).items():
        field = PAPER_SECTIONS.get(title)
        if field is not None:
            by_field.setdefault(field, []).extend(lines)
    fields: dict[str, Any] = {}
    proposal = _text(_drop_label(by_field.get("proposal", []), "Beschlussvorschlag"))
    if proposal:
        fields["mandari:proposal"] = proposal
    explanation = _text(_drop_label(by_field.get("explanation", []), "Sachverhalt"))
    if explanation:
        fields["mandari:explanation"] = explanation
    return fields
