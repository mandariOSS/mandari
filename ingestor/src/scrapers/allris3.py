# SPDX-FileCopyrightText: 2020, 2021 Timo B. Kranz (https://github.com/bv-hh/bv-hh)
# SPDX-License-Identifier: AGPL-3.0-or-later AND MIT
"""
ALLRIS-3-Adapter (CC e-gov ALLRIS net 3.x, klassische ASP-Oberfläche unter ``/bi/*.asp``), Issue #121.

Geprüft an Seiten von ALLRIS net 3.9.5 (Testseiten unter ``tests/fixtures/scrapers/allris3/``). Vorbild für
Seitenauswahl und Selektoren ist das Projekt bv-hh (https://github.com/bv-hh/bv-hh), von dort stammen auch die
Testseiten. Die Zeichentabelle der Schriften Symbol und Wingdings (``SYMBOL_FONT_CHARACTERS``) und die
Bezeichnungen der Abschnitte einer Drucksache sind aus bv-hh übernommen (``app/models/concerns/parsing.rb``,
``app/models/document.rb``) und stehen unter der MIT-Lizenz (``LICENSES/MIT.txt``, Urheber Timo B. Kranz, siehe
Dateikopf); der übrige Code unter AGPL-3.0-or-later.

Seitenkürzel (``<Basis-URL><Kürzel>.asp``, Basis meist ``…/bi/``):

- ``si010_e``  Sitzungskalender eines Monats (``MM``, ``YY``)
- ``to010``    Sitzung mit Tagesordnung (``SILFDNR``)
- ``to020``    Tagesordnungspunkt mit Protokoll und Beschluss (``TOLFDNR``)
- ``vo020``    Drucksache bzw. Vorlage (``VOLFDNR``) mit Sachverhalt und Beschlussvorschlag
- ``vo040``    Drucksachenliste, neueste zuerst
- ``au020``    Ausschuss mit Mitgliedern (``AULFDNR``)
- ``pa020``    Hauptorgan (Rat, Kreistag, Bezirksversammlung) mit Mitgliedern (``PALFDNR``)
- ``fr010``    Fraktionsliste, ``fr020`` Fraktion mit Mitgliedern (``FRLFDNR``)
- ``kp020``    Person (``KPLFDNR``): nicht abgerufen, der Name steht in den Mitgliederlisten
- ``do027``    Dokumente (Einladung, Niederschrift, Drucksache als PDF): nur per POST-Formular mit Sitzungs-Cookie,
               Anlagen nur als befristete Kopie unter ``___tmp/``. Beides trägt die Datei-Pipeline nicht (GET auf eine
               dauerhafte Adresse, Löschabgleich über 404/410); der Adapter legt deshalb keine Dateien an.

external_id = kanonische Detail-URL mit dem ``*LFDNR``-Parameter (``normalize_external_id``). Tagesordnungspunkte
haben mit ``TOLFDNR`` eine eigene, stabile Kennung (``to020.asp?TOLFDNR=N``), auch ohne veröffentlichte Detailseite.

ALLRIS 3 deklariert ISO-8859-1, schreibt aber Windows-1252 (typografische Striche und Anführungszeichen in 0x80–0x9F);
die Seiten werden aus den Rohbytes dekodiert (``decode_page``). Inhalte aus Word („ALLRIS Office Integration“)
werden zu Text mit Zeilenumbrüchen.

„Nicht erreichbar ist nicht gelöscht“: Nicht öffentliche Seiten (Weiterleitung auf ``noauth.asp``, „Keine Information
verfügbar“) zählen wie ein toter Verweis, gestörte und fremde Seiten nie als leere Liste (``ScrapeStats``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction, Tag
from mandari_oparl.abgleich import detect_gate, is_gone

from src.metrics import metrics
from src.redaction import MaskingConsole
from src.scrapers.base import CrawlWindow, ScraperConfig, ScrapeStats, normalize_external_id, with_content_hash
from src.scrapers.politeness import PoliteFetcher, RobotsDisallowedError

console = MaskingConsole()

OPARL = "https://schema.oparl.org/1.1/"
TZ_BERLIN = ZoneInfo("Europe/Berlin")

#: Kennungsparameter der Detailseiten (Groß- und Kleinschreibung wie bei ALLRIS)
ID_PARAMS = ("SILFDNR", "TOLFDNR", "VOLFDNR", "AULFDNR", "PALFDNR", "FRLFDNR", "KPLFDNR")

#: So viele Drucksachen gibt der Adapter höchstens in einer Seite an den Läufer weiter (Rückwärtszählen)
PAPER_PAGE_SIZE = 50
#: So viele Kennungen fragt ein Vollabgleich beim Rückwärtszählen höchstens ab, auch ohne ``max_detail_pages``
#: (``scraper.paper_backfill_per_run``); spätere Läufe setzen beim gemerkten Stand fort
PAPER_BACKFILL_PER_RUN = 200
#: Stand des Rückwärtszählens in ``scraper_state.list_snapshots``: nächste abzufragende Kennung
BACKFILL_KEY = "vo020_backfill"
#: Neueste Kennung der Liste vo040 beim letzten Lauf (``scraper_state.list_snapshots``)
LIST_HEAD_KEY = "vo040"

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})(?:\s*[-–]\s*(\d{1,2}:\d{2}))?")
_CONTAINER_RE = re.compile(r"<div\b[^>]*\bid\s*=\s*[\"']allriscontainer[\"'][^>]*>", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
#: Seite einer nicht öffentlichen Drucksache bzw. Sitzung (bv-hh: ``Document::NON_PUBLIC``)
_NOT_PUBLIC_RE = re.compile(r"Keine\s+Information\s+verf(?:&uuml;|ü)gbar", re.IGNORECASE)
_NOAUTH = "noauth.asp"
_WEEKDAYS = {"mo", "di", "mi", "do", "fr", "sa", "so"}
_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
#: Ö/N vor der Nummer eines Tagesordnungspunkts: öffentlich bzw. nicht öffentlich
_TOP_MARKER_RE = re.compile(r"^([ÖöNn])\s+")

#: Word schreibt Zeichen der Schriften Symbol und Wingdings als Private Use Area (U+F000 plus Zeichencode), die keine
#: Schrift darstellt. Zuordnung wie in bv-hh (``Parsing::SYMBOL_FONT_CHARACTERS``).
SYMBOL_FONT_CHARACTERS = {
    # Symbol
    "\uf020": " ",
    "\uf028": "(",
    "\uf029": ")",
    "\uf02a": "*",
    "\uf02d": "–",
    "\uf02e": ".",
    "\uf05b": "[",
    "\uf05d": "]",
    "\uf0ae": "→",
    "\uf0b7": "•",
    "\uf0de": "⇒",
    **{chr(0xF030 + digit): str(digit) for digit in range(10)},
    # Wingdings
    "\uf075": "◆",
    "\uf076": "❖",
    "\uf0a0": "▪",
    "\uf0a7": "▪",
    "\uf0d8": "➢",
    "\uf0e0": "→",
    "\uf0e8": "➔",
    "\uf0f0": "⇨",
}
_SYMBOL_FONT_TABLE = str.maketrans(SYMBOL_FONT_CHARACTERS)

#: Anfang eines Abschnitts mit Sachverhalt bzw. Beschlussvorschlag, Anlagen (bv-hh: ``Document::SECTION_LABELS``)
_LABEL_EXPLANATION_RE = re.compile(r"^(?:Sachverhalt(?:\s+und\s+Petitum)?\b\s*:?|Hintergrund\s*:)\s*")
_LABEL_PROPOSAL_RE = re.compile(r"^(?:Petitum\b(?:\s*/\s*(?:Beschluss\w*)?)?\s*:?|Beschluss(?:vorschlag)?\s*:)\s*")
_LABEL_ATTACHMENTS_RE = re.compile(r"^Anlagen?(?:/n)?\s*:")
#: Platzhalter eines leeren Abschnitts: „keine“, „ohne“, „---“, „-/-“
_PLACEHOLDER_RE = re.compile(r"^(?:ohne|keine|[-–—/]+)\.?$", re.IGNORECASE)
#: Anker der Abschnitte einer Seite (``<a name="allrisSV"></a>``)
_SECTION_ANCHOR_RE = re.compile(r"<a\s+name\s*=\s*[\"']allris(\w+)[\"']\s*/?>(?:\s*</a>)?", re.IGNORECASE)
#: Ende des letzten Abschnitts: Anlagentabelle oder Ende der Inhaltszelle
_SECTION_END_RE = re.compile(r"<table\b[^>]*\bclass\s*=\s*[\"']tk1[\"']|</td>\s*<td\b[^>]*background", re.IGNORECASE)

#: Abschnitte der Mitgliederliste bzw. Arten der Mitarbeit ohne Stimmrecht (wie SessionNet, Issue #1075)
_NO_VOTE_RE = re.compile(r"ohne stimmrecht|beratend|hinzugezogen|^verwaltung\b", re.IGNORECASE)

_BLOCK_TAGS = [
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "tr",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "hr",
    "dd",
    "dt",
]


# ---------------------------------------------------------------------------
# Zeichensatz und Seitenart
# ---------------------------------------------------------------------------


def decode_page(raw: bytes) -> str:
    """
    Rohbytes einer ALLRIS-Seite als Text.

    ALLRIS 3 deklariert ISO-8859-1, schreibt aber Windows-1252: Gedankenstriche und Anführungszeichen kommen als
    0x80–0x9F, die ISO-8859-1 zu Steuerzeichen machte. Sonst stimmen beide überein. Eine Seite, die schon gültiges
    UTF-8 ist, bleibt, wie sie ist (deutscher Text in Windows-1252 ist praktisch nie gültiges UTF-8).
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


_C1_RE = re.compile("[\x80-\x9f]")


def repair_c1(text: str) -> str:
    """Text, den ein HTTP-Client als ISO-8859-1 gelesen hat: Steuerzeichen 0x80–0x9F sind Zeichen aus Windows-1252."""
    return _C1_RE.sub(lambda m: bytes([ord(m.group())]).decode("cp1252", errors="replace"), text)


def page_kind(html: str) -> str | None:
    """
    Art der ALLRIS-Seite aus der Klasse des Rahmens ``div#allriscontainer`` (``vo020``, ``to010``, ``si010`` …).

    ``None``: keine ALLRIS-Seite (Sperr-, Prüf- oder Hinweisseite); ``""``: Rahmen ohne Klasse.
    """
    match = _CONTAINER_RE.search(html)
    if match is None:
        return None
    css = _CLASS_ATTR_RE.search(match.group(0))
    return css.group(1).split()[0].lower() if css and css.group(1).split() else ""


def kind_matches(found: str | None, expected: str) -> bool:
    """
    Ist die Seite genau die angefragte Seitenart? Der Rahmen nennt die Seite ohne Zusatz (der Kalender ``si010_e``
    trägt ``si010``, so wird er angefragt). Eine Sitzung (``to010``) ist kein Tagesordnungspunkt (``to020``).
    """
    return found is not None and found == expected


def is_noauth_url(url: str | None) -> bool:
    """Ist die Adresse die Anmeldeseite von ALLRIS (``noauth.asp``)?"""
    return bool(url) and urlparse(str(url)).path.lower().endswith("/" + _NOAUTH)


def _outside_sections(html: str) -> str:
    """
    Seite ohne die Abschnitte aus Word (Sachverhalt, Wortprotokoll …, ab dem ersten ``allris*``-Anker): Dort darf
    jeder Text stehen, auch „Keine Information verfügbar“.
    """
    first = _SECTION_ANCHOR_RE.search(html)
    return html[: first.start()] if first else html


def is_not_public(html: str, url: str | None = None, expected: str | None = None) -> bool:
    """
    Zeigt ALLRIS statt der Seite „nicht öffentlich“?

    - Weiterleitung auf die Anmeldung (Adresse ``noauth.asp``) oder deren Seite (Rahmen ``noauth``);
    - eine Seite ohne Rahmen, die auf die Anmeldung verweist oder „Keine Information verfügbar“ sagt;
    - die angefragte Seitenart (``expected``) mit „Keine Information verfügbar“ außerhalb der Abschnitte aus Word.

    Eine Seite einer anderen Art ist nie „nicht öffentlich“, sondern nicht lesbar: Sonst entfielen gespeicherte
    Protokolle und Beschlüsse.
    """
    if is_noauth_url(url):
        return True
    kind = page_kind(html)
    if kind == "noauth":
        return True
    if kind is None:
        return _NOAUTH in html.lower() or _NOT_PUBLIC_RE.search(html) is not None
    if expected is not None and kind != expected:
        return False
    return _NOT_PUBLIC_RE.search(_outside_sections(html)) is not None


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _query_int(href: str, param: str) -> int | None:
    """Ganzzahliger Query-Parameter aus einem (relativen) Link, Name ohne Rücksicht auf Groß- und Kleinschreibung."""
    try:
        query = parse_qs(urlparse(href.replace("&amp;", "&")).query)
    except ValueError:
        return None
    for key, values in query.items():
        if key.lower() == param.lower() and values:
            try:
                return int(values[0])
            except ValueError:
                return None
    return None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _text(tag: Any) -> str:
    """Einzeiliger Text eines Felds (Zeilenumbrüche und ``<br>`` werden Leerzeichen, Skripte entfallen)."""
    if tag is None:
        return ""
    for script in tag.find_all(["script", "style"]):
        script.decompose()
    return _clean(tag.get_text(" "))


def _attr(tag: Any, name: str) -> str:
    value = tag.get(name) if tag is not None else None
    if isinstance(value, list):
        return " ".join(value)
    return str(value or "")


def _iso_date(value: str | None) -> str | None:
    """``Mo, 03.11.2025`` bzw. ``3.11.2025`` -> ``2025-11-03``."""
    match = _DATE_RE.search(value or "")
    if match is None:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def _times(value: str | None) -> tuple[str | None, str | None]:
    """``18:00 - 19:45`` -> (``18:00``, ``19:45``)."""
    match = _TIME_RANGE_RE.search(value or "")
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def _datetime(iso_date: str | None, time: str | None) -> str | None:
    """ISO-Datum + ``HH:MM`` -> ISO 8601 mit Versatz für Europe/Berlin."""
    if not iso_date:
        return None
    try:
        value = datetime.fromisoformat(iso_date)
        if time:
            hour, minute = time.split(":")
            value = value.replace(hour=int(hour), minute=int(minute))
    except ValueError:
        return None
    return value.replace(tzinfo=TZ_BERLIN).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "unbekannt"


def _heading(soup: BeautifulSoup) -> str:
    """Überschrift der Seite (``div#risname h1``) ohne geschützte Leerzeichen am Ende."""
    h1 = soup.select_one("#risname h1") or soup.find("h1")
    return _text(h1)


def _after_dash(value: str) -> str | None:
    """``Drucksache - 22-2362`` -> ``22-2362``; ohne Trenner ``None``."""
    _, sep, rest = value.partition(" - ")
    return (_clean(rest) or None) if sep else None


def _header_fields(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Kopftabelle einer Detailseite: Bezeichnung (``td.kb1``, ohne Doppelpunkt) -> Wertzelle (folgendes ``td``).

    Gelesen wird nur die Kopfzelle (``td.ko1``) der Seite, die erste Nennung einer Bezeichnung zählt.
    """
    root = soup.select_one("table.risdeco td.ko1") or soup.select_one("td.ko1") or soup
    fields: dict[str, Any] = {}
    for label_cell in root.find_all("td", class_="kb1"):
        label = _clean(label_cell.get_text()).rstrip(":").strip()
        value = label_cell.find_next_sibling("td")
        if label and value is not None and label not in fields:
            fields[label] = value
    return fields


# ---------------------------------------------------------------------------
# Word-HTML zu Text
# ---------------------------------------------------------------------------


def html_to_text(fragment: str | Tag) -> str:
    """
    Text eines Abschnitts aus Word-HTML: Absätze, Listenpunkte und Tabellenzeilen als eigene Zeilen, Zellen durch
    Leerzeichen getrennt. Word zerlegt Text in Spans, oft mitten im Wort und an Umlauten; Spans werden deshalb ohne
    Trenner zusammengesetzt. Zeichen der Schriften Symbol und Wingdings werden lesbar (``SYMBOL_FONT_CHARACTERS``).
    Ein übergebenes Element wird dabei verändert (Zeilenumbrüche eingefügt, Skripte entfernt).
    """
    soup = BeautifulSoup(fragment, "html.parser") if isinstance(fragment, str) else fragment
    for node in soup.find_all(string=lambda s: isinstance(s, Comment | Declaration | Doctype | ProcessingInstruction)):
        node.extract()
    for tag in soup.find_all(["script", "style", "head", "title", "form", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" ")
    text = soup.get_text().replace("\xa0", " ").translate(_SYMBOL_FONT_TABLE)
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _sections(html: str) -> list[tuple[str, str]]:
    """
    Abschnitte einer Seite nach den Ankern von ALLRIS (``allrisSV`` Sachverhalt, ``allrisBV`` Beschlussvorschlag,
    ``allrisAN`` Anlagen, ``allrisWP`` Wortprotokoll, ``allrisBS`` Beschluss, ``allrisAE`` Abstimmungsergebnis):
    (Kürzel, HTML bis zum nächsten Anker bzw. bis zur Anlagentabelle oder zum Ende der Inhaltszelle).
    """
    anchors = list(_SECTION_ANCHOR_RE.finditer(html))
    sections: list[tuple[str, str]] = []
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(html)
        stop = _SECTION_END_RE.search(html, anchor.end(), end)
        if stop is not None:
            end = stop.start()
        sections.append((anchor.group(1).upper(), html[anchor.end() : end]))
    return sections


def _top_blocks(fragment: str) -> list[Tag | str]:
    """Oberste ``div`` eines Abschnitts (je ein Block aus Word); ohne ``div`` der ganze Abschnitt."""
    soup = BeautifulSoup(fragment, "html.parser")
    blocks = [div for div in soup.find_all("div") if div.find_parent("div") is None]
    return list(blocks) or [fragment]


def _meaningful(text: str) -> bool:
    return bool(text) and _PLACEHOLDER_RE.match(text) is None


def section_texts(html: str, codes: Iterable[str]) -> str | None:
    """Text der Abschnitte mit diesen Kürzeln in Seitenreihenfolge; ``None`` ohne Inhalt (leere Absätze)."""
    wanted = {code.upper() for code in codes}
    parts = [html_to_text(body) for code, body in _sections(html) if code in wanted]
    text = "\n".join(part for part in parts if _meaningful(part))
    return text or None


def paper_texts(html: str) -> tuple[str | None, str | None]:
    """
    Sachverhalt und Beschlussvorschlag (Petitum) einer Drucksache (vo020).

    Wie in bv-hh entscheidet die Bezeichnung am Anfang eines Blocks, nicht der Anker: „Petitum/Beschluss:“,
    „Beschluss:“ und „Beschlussvorschlag:“ gehören zum Beschlussvorschlag, „Anlage/n:“ entfällt, alles andere
    (auch Blöcke ohne Bezeichnung, etwa eine Antwort der Verwaltung oder Fußnoten) zum Sachverhalt. Bezeichnungen
    werden entfernt, Platzhalter („keine“, „---“) gelten als leer.
    """
    explanation: list[str] = []
    proposal: list[str] = []
    for _code, body in _sections(html):
        for block in _top_blocks(body):
            text = html_to_text(block)
            if not text or _LABEL_ATTACHMENTS_RE.match(text):
                continue
            match = _LABEL_PROPOSAL_RE.match(text)
            target = proposal
            if match is None:
                match = _LABEL_EXPLANATION_RE.match(text)
                target = explanation
            if match is not None:
                text = text[match.end() :].strip()
            if _meaningful(text):
                target.append(text)
    return ("\n".join(explanation) or None, "\n".join(proposal) or None)


# ---------------------------------------------------------------------------
# Parser (reine Funktionen, mit Golden Files getestet)
# ---------------------------------------------------------------------------


@dataclass
class MeetingStub:
    """Eintrag des Sitzungskalenders (si010_e)."""

    silfdnr: int
    name: str
    date: str  # ISO
    time_start: str | None = None
    time_end: str | None = None
    room: str | None = None
    #: Verweist der Eintrag auf die Tagesordnung (to010)? Ohne Verweis nur die Angaben des Kalenders.
    agenda_linked: bool = True
    #: Steht am Eintrag die Niederschrift (Formular ``si016``)? Ändert sich mit dem Protokoll, also den Monat neu lesen.
    minutes: bool = False

    def sort_key(self) -> tuple[str, int]:
        return (self.date, self.silfdnr)


def calendar_month(html: str) -> tuple[int, int] | None:
    """(Jahr, Monat) aus der Überschrift des Kalenders („Oktober 2023“); ``None``, wenn nicht erkennbar."""
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all("h2"):
        words = _clean(heading.get_text()).lower().split()
        if len(words) == 2 and words[0] in _MONTHS and words[1].isdigit():
            return int(words[1]), _MONTHS[words[0]]
    return None


def is_calendar_page(html: str) -> bool:
    """Trägt die Seite den Monatskalender? Er hat je Tag eine Zeile mit Wochentag, auch an Tagen ohne Sitzung."""
    soup = BeautifulSoup(html, "html.parser")
    days = 0
    for row in soup.select("table.tl1 tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) >= 2 and _clean(cells[0].get_text()).lower() in _WEEKDAYS:
            days += 1
    return days >= 28


def parse_calendar(html: str, year: int, month: int) -> list[MeetingStub]:
    """
    Sitzungen eines Monats aus dem Kalender (si010_e).

    Jede Sitzung trägt ihre Nummer (``SILFDNR``) im Formular „Termin exportieren“, auch ohne veröffentlichte
    Tagesordnung; Zeilen ohne Nummer (Ferien, Feiertage) fehlen. Weitere Sitzungen desselben Tages stehen in
    Zeilen ohne Tagesangabe.
    """
    soup = BeautifulSoup(html, "html.parser")
    stubs: list[MeetingStub] = []
    seen: set[int] = set()
    day: int | None = None
    for row in soup.select("table.tl1 tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        day_text = _clean(cells[1].get_text())
        if day_text.isdigit():
            day = int(day_text)
        link = row.find("a", href=re.compile(r"to010\.asp\?", re.IGNORECASE))
        number = _query_int(_attr(link, "href"), "SILFDNR") if link is not None else None
        if number is None:
            field_input = row.find("input", attrs={"name": re.compile(r"^SILFDNR$", re.IGNORECASE)})
            value = _attr(field_input, "value") if field_input is not None else ""
            number = int(value) if value.isdigit() else None
        if number is None or day is None or number in seen:
            continue
        try:
            iso = date(year, month, day).isoformat()
        except ValueError:
            continue
        name = _text(link) if link is not None else (_text(cells[5]) if len(cells) > 5 else "")
        if not name:
            continue
        start, end = _times(_text(cells[2])) if len(cells) > 2 else (None, None)
        rooms = row.find_all("td", class_="text4")
        room = _text(rooms[-1]) if rooms else ""
        minutes = row.find("form", action=re.compile(r"si016\.asp", re.IGNORECASE)) is not None
        seen.add(number)
        stubs.append(
            MeetingStub(
                silfdnr=number,
                name=name,
                date=iso,
                time_start=start,
                time_end=end,
                room=room or None,
                agenda_linked=link is not None,
                minutes=minutes,
            )
        )
    return stubs


@dataclass
class AgendaRow:
    """Ein Tagesordnungspunkt aus to010."""

    tolfdnr: int | None
    number: str | None
    title: str
    public: bool = True
    #: Beschlussart, wie die Tagesordnung sie nennt („ungeändert beschlossen“); ``None`` solange „(offen)“
    decision: str | None = None
    #: Hat der Punkt eine Detailseite (to020)?
    detail: bool = False
    volfdnr: int | None = None
    paper_reference: str | None = None


@dataclass
class MeetingPage:
    """Kopf und Tagesordnung einer Sitzung (to010)."""

    title: str | None = None
    #: ("au" | "pa", Nummer, Name) aus dem Verweis „Gremium“
    committee: tuple[str, int, str] | None = None
    date: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    room: str | None = None
    address: str | None = None
    rows: list[AgendaRow] = field(default_factory=list)


def _committee_link(cell: Any) -> tuple[str, int, str] | None:
    link = cell.find("a", href=re.compile(r"(au|pa)020\.asp", re.IGNORECASE)) if cell is not None else None
    if link is None:
        return None
    href = _attr(link, "href")
    for kind, param in (("au", "AULFDNR"), ("pa", "PALFDNR")):
        number = _query_int(href, param)
        if number is not None:
            name = _text(link)
            return (kind, number, name) if name else None
    return None


def parse_meeting(html: str) -> MeetingPage:
    """Sitzung mit Tagesordnung (to010): Bezeichnung, Gremium, Datum, Zeit, Raum, Ort und Tagesordnungspunkte."""
    soup = BeautifulSoup(html, "html.parser")
    page = MeetingPage()
    fields = _header_fields(soup)
    page.title = _text(fields.get("Bezeichnung")) or _after_dash(_heading(soup))
    page.committee = _committee_link(fields.get("Gremium"))
    page.date = _iso_date(_text(fields.get("Datum")))
    page.time_start, page.time_end = _times(_text(fields.get("Zeit")))
    page.room = _text(fields.get("Raum")) or None
    page.address = _text(fields.get("Ort")) or None

    for row in soup.select("table.tl1 tr.zl11, table.tl1 tr.zl12"):
        cells = row.find_all("td", recursive=False)
        number_cell = row.find("td", class_="text4")
        if number_cell is None or len(cells) < 4:
            continue
        raw_number = _text(number_cell)
        if not raw_number:
            continue
        marker = _TOP_MARKER_RE.match(raw_number)
        number = (raw_number[marker.end() :] if marker else raw_number).strip() or None
        detail_link = row.find("a", href=re.compile(r"to020\.asp\?", re.IGNORECASE))
        title = _text(detail_link) if detail_link is not None else _text(cells[3])
        if not title:
            continue
        tolfdnr = None
        for candidate in (row.find("a", href=re.compile(r"TOLFDNR=", re.IGNORECASE)), detail_link):
            if candidate is not None and tolfdnr is None:
                tolfdnr = _query_int(_attr(candidate, "href"), "TOLFDNR")
        top_input = row.find("input", attrs={"name": re.compile(r"^TOLFDNR$", re.IGNORECASE)})
        if tolfdnr is None and top_input is not None and _attr(top_input, "value").isdigit():
            tolfdnr = int(_attr(top_input, "value"))
        detail_form = row.find("form", action=re.compile(r"to020\.asp", re.IGNORECASE))
        decision = None
        if detail_form is not None:
            submit = detail_form.find("input", attrs={"type": "submit"})
            decision = _clean(_attr(submit, "title")) or None
        if decision and decision.strip("()").lower() == "offen":
            decision = None
        item = AgendaRow(
            tolfdnr=tolfdnr,
            number=number,
            title=title,
            public=True if marker is None else marker.group(1) in "Öö",
            decision=decision,
            detail=detail_link is not None or detail_form is not None,
        )
        paper = row.find("a", href=re.compile(r"vo020\.asp\?.*VOLFDNR=", re.IGNORECASE))
        if paper is not None:
            item.volfdnr = _query_int(_attr(paper, "href"), "VOLFDNR")
            item.paper_reference = _text(paper) or None
        page.rows.append(item)
    return page


def parse_agenda_item(html: str) -> dict[str, str | None]:
    """
    Tagesordnungspunkt (to020): Wortprotokoll (``allrisWP``) sowie Beschluss und Abstimmungsergebnis (``allrisBS``,
    ``allrisAE``, in Seitenreihenfolge). Leere Absätze, wie sie bis zum Protokoll dastehen, ergeben ``None``.
    """
    return {"minutes": section_texts(html, ["WP"]), "resolution": section_texts(html, ["BS", "AE"])}


def parse_paper(html: str) -> dict[str, Any]:
    """
    Drucksache (vo020): Nummer (Überschrift), Betreff, Art, Datum (falls genannt), Sachverhalt und
    Beschlussvorschlag (``paper_texts``).
    """
    # Kopf ohne die Abschnitte aus Word: Die sind der größte Teil der Seite und werden eigens gelesen
    first_section = _SECTION_ANCHOR_RE.search(html)
    soup = BeautifulSoup(html[: first_section.start()] if first_section else html, "html.parser")
    fields = _header_fields(soup)
    info: dict[str, Any] = {"reference": _after_dash(_heading(soup)), "name": _text(fields.get("Betreff")) or None}
    paper_type = next((_text(cell) for label, cell in fields.items() if label.lower().endswith("art")), "")
    info["paper_type"] = paper_type or None
    date_cell = next((cell for label, cell in fields.items() if label.lower() == "datum"), None)
    info["date"] = _iso_date(_text(date_cell))
    info["explanation"], info["proposal"] = paper_texts(html)
    return info


def parse_paper_list(html: str) -> list[dict[str, Any]]:
    """Drucksachenliste (vo040): Nummer, Kennung, Betreff, Datum und Art je Zeile, neueste zuerst."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in soup.select("table.tl1 tr.zl11, table.tl1 tr.zl12"):
        link = row.find("a", href=re.compile(r"vo020\.asp\?.*VOLFDNR=", re.IGNORECASE))
        if link is None:
            continue
        volfdnr = _query_int(_attr(link, "href"), "VOLFDNR")
        if volfdnr is None or volfdnr in seen:
            continue
        seen.add(volfdnr)
        reference, _, name = link.get_text().replace("\xa0", " ", 1).partition(" ")
        details = [_text(cell) for cell in row.find_all("td", class_="text2")]
        rows.append(
            {
                "volfdnr": volfdnr,
                "reference": _clean(reference) or None,
                "name": _clean(name) or None,
                "date": _iso_date(details[0]) if details else None,
                "paper_type": details[1] if len(details) > 1 and details[1] else None,
            }
        )
    return rows


def parse_factions(html: str) -> list[dict[str, Any]]:
    """Fraktionsliste (fr010): Nummer (``FRLFDNR``) und Name."""
    soup = BeautifulSoup(html, "html.parser")
    factions: dict[int, dict[str, Any]] = {}
    for link in soup.select("table.tl1 a[href]"):
        if not re.search(r"fr020\.asp\?", _attr(link, "href"), re.IGNORECASE):
            continue
        number = _query_int(_attr(link, "href"), "FRLFDNR")
        name = _text(link)
        if number is not None and name:
            factions.setdefault(number, {"frlfdnr": number, "name": name})
    return list(factions.values())


def parse_members(html: str) -> dict[str, Any]:
    """
    Gremium bzw. Fraktion mit Mitgliedern (au020, pa020, fr020): Name (Überschrift) und je Mitglied ``KPLFDNR``,
    Name, Art der Mitarbeit (``role``) und Fraktion bzw. Funktion (``faction``).

    Gelesen wird die erste Liste mit Personenverweisen (``kp020``). Mitglieder ohne Personenseite (etwa zubenannte
    Bürger:innen) stehen nur mit Namen darin.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = next((t for t in soup.select("table.tl1") if t.find("a", href=re.compile(r"kp020\.asp", re.I))), None)
    members: list[dict[str, Any]] = []
    for row in table.select("tr.zl11, tr.zl12") if table is not None else []:
        cells = row.find_all("td", recursive=False)
        link = row.find("a", href=re.compile(r"kp020\.asp\?", re.IGNORECASE))
        if link is not None:
            kplfdnr = _query_int(_attr(link, "href"), "KPLFDNR")
            name = _text(link)
        else:
            kplfdnr = None
            name = _text(cells[2]) if len(cells) > 2 else ""
        if not name:
            continue
        role = _text(row.find("td", class_="text1")) or None
        members.append(
            {
                "kplfdnr": kplfdnr,
                "name": name,
                "role": role,
                "faction": _text(row.find("td", class_="text4")) or None,
                "voting_right": not (role and _NO_VOTE_RE.search(role)),
            }
        )
    return {"name": _heading(soup) or None, "members": members}


def _member_key(row: dict[str, Any]) -> str:
    return str(row["kplfdnr"]) if row.get("kplfdnr") is not None else _slug(str(row.get("name") or ""))


def membership_keys(rows: list[dict[str, Any]]) -> list[str]:
    """Kennungsteile der Mitgliedschaften hinter ``#membership/``: die Person, bei Wiederholung mit Zählung (``-2``)."""
    used: set[str] = set()
    keys: list[str] = []
    for row in rows:
        base = _member_key(row)
        key, count = base, 1
        while key in used:
            count += 1
            key = f"{base}-{count}"
        used.add(key)
        keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class Allris3Urls:
    """Seitenadressen und kanonische Kennungen einer Instanz."""

    base_url: str  # mit Schrägstrich am Ende, z. B. https://ris.example.org/bi/

    def page(self, name: str, **params: Any) -> str:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        url = f"{self.base_url}{name}.asp"
        return f"{url}?{query}" if query else url

    def external_id(self, name: str, **params: Any) -> str:
        return normalize_external_id(self.page(name, **params), keep_params=ID_PARAMS)

    def body_id(self) -> str:
        return normalize_external_id(self.base_url, keep_params=())


@dataclass
class _MeetingResult:
    meeting: dict[str, Any]
    consultations: list[dict[str, Any]]
    papers: list[int]
    committee: dict[str, Any] | None = None


class _Kept:
    """Sitzung, deren Tagesordnung ALLRIS nicht herausgibt: Sie bleibt im Bestand, wie sie ist."""


_KEPT = _Kept()


def _int_option(value: Any, name: str) -> int | None:
    """Ganzzahl größer 0 aus ``sync_config["scraper"]``; sonst ``None`` mit Warnung (der Crawl läuft weiter)."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        console.print(f"[yellow]ALLRIS 3: scraper.{name}={value!r} ist keine Zahl größer 0 – ignoriert[/yellow]")
        return None
    return number


class Allris3Adapter:
    """
    Crawlt eine ALLRIS-3-Instanz und liefert synthetische OParl-Dicts.

    Reihenfolge (FK-kompatibel): Fraktionen (fr010) mit Personen und Mitgliedschaften (fr020, nur im Vollabgleich),
    danach je Kalendermonat die Gremien der Sitzungen (mit Mitgliedern aus au020/pa020 im Vollabgleich),
    Drucksachen der Tagesordnungen, Sitzungen mit Tagesordnungspunkten und Beratungen, zuletzt neue Drucksachen der
    Liste vo040 und im Vollabgleich die Kennungen rückwärts bis ``scraper.oldest_paper_id``.

    Gremien kennt ALLRIS 3 ohne eigene Liste nur über ihre Sitzungen; der Löschabgleich gilt deshalb nur für
    Sitzungen im Fenster und Tagesordnungspunkte gelesener Sitzungen (``tombstone_entity_types``).
    """

    vendor = "allris3"
    schema_version = 1
    #: Löschabgleich nur für Sitzungen; ein Gremium ohne Sitzung im Fenster fehlt nicht in der Quelle
    tombstone_entity_types = ("meeting",)

    def __init__(self, config: ScraperConfig, fetcher: PoliteFetcher, today: date | None = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self.urls = Allris3Urls(base_url=config.base_url)
        self.stats = ScrapeStats()
        #: Protokolle gibt es erst nach der Sitzung: to020 nur für Sitzungen vor diesem Tag
        self.today = today or datetime.now(TZ_BERLIN).date()
        self._crawl_time = datetime.now(TZ_BERLIN).isoformat(timespec="seconds")
        #: Untergrenze der Drucksachen-Kennungen beim Rückwärtszählen im Vollabgleich (``scraper.oldest_paper_id``)
        self.oldest_paper_id = _int_option(config.extra.get("oldest_paper_id"), "oldest_paper_id")
        #: Höchstens so viele Kennungen je Vollabgleich beim Rückwärtszählen (``scraper.paper_backfill_per_run``)
        self.paper_backfill_per_run = (
            _int_option(config.extra.get("paper_backfill_per_run"), "paper_backfill_per_run") or PAPER_BACKFILL_PER_RUN
        )
        #: Listen-Diffing wie bei SessionNet: Monat -> Hash der Kalendereinträge, ``vo040`` -> neueste Kennung,
        #: ``vo020_backfill`` -> nächste Kennung beim Rückwärtszählen
        self.list_snapshots: dict[str, str] = {}
        #: Stände des letzten Laufs, die der Läufer im Änderungsabgleich setzt (im Vollabgleich leer)
        self.previous_snapshots: dict[str, str] = {}
        #: Gespeicherte Stände unabhängig von der Art des Laufs (Läufer), für den Fortschritt beim Rückwärtszählen
        self.stored_snapshots: dict[str, str] = {}
        #: War die zuletzt geholte Seite „nicht öffentlich“?
        self.last_not_public = False
        self._orgs_yielded: set[str] = set()
        self._persons_yielded: set[str] = set()
        self._members_read: set[str] = set()

    # -------------------- Abruf --------------------

    async def _fetch(
        self,
        url: str,
        kind: str,
        *,
        is_detail: bool = False,
        expected: Callable[[str], bool] | None = None,
    ) -> str | None:
        """
        Seite holen und als Text dekodieren; ``None``, wenn sie nicht lesbar ist.

        Eine Seite ohne ALLRIS-Rahmen (Sperr-, Prüf- oder Hinweisseite) oder mit einer anderen Seitenart wird nicht
        ausgewertet, ``expected`` prüft zusätzlich ihren Aufbau. Nicht öffentlich (``noauth.asp``, „Keine Information
        verfügbar“) zählt wie ein toter Verweis: Die Quelle gibt die Seite bewusst nicht heraus.
        """
        if is_detail:
            self.stats.detail_pages_attempted += 1
        self.last_not_public = False
        try:
            text = await self.fetcher.fetch_text(url)
        except RobotsDisallowedError as error:
            # Weiterleitung auf die Anmeldung, deren Adresse die robots.txt sperrt: nicht abgerufen, und die Seite ist
            # nicht öffentlich. Jede andere Sperre bricht den Lauf ab (Läufer, Fehlerklasse robots_blocked).
            if str(error) == url or not is_noauth_url(str(error)):
                raise
            text = None
            self.last_not_public = True
        html: str | None = None
        if text is not None:
            raw = getattr(self.fetcher, "last_content", None)
            html = decode_page(raw) if isinstance(raw, bytes) else repair_c1(text)
        if html is not None:
            found = page_kind(html)
            final_url = getattr(self.fetcher, "last_url", None)
            if is_not_public(html, final_url if isinstance(final_url, str) else None, expected=kind):
                console.print(f"[dim]ALLRIS 3: nicht öffentlich: {url}[/dim]")
                self.last_not_public = True
                html = None
            elif found is None:
                art = detect_gate(200, html) or "fremder Aufbau"
                console.print(f"[yellow]Scraper: keine ALLRIS-Seite ({art}), nicht ausgewertet: {url}[/yellow]")
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "fremde_seite")
                html = None
            elif not kind_matches(found, kind) or (expected is not None and not expected(html)):
                console.print(f"[yellow]Scraper: Seite ohne erwarteten Aufbau ({found}): {url}[/yellow]")
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "fremde_seite")
                html = None
        if html is None:
            self.stats.failed_pages += 1
            if self.last_not_public or is_gone(getattr(self.fetcher, "last_status", None)):
                self.stats.gone_pages += 1
        else:
            self.stats.pages_fetched += 1
        return html

    def _disturbances(self) -> int:
        """Bisher gestörte Seiten (ohne tote Verweise und nicht öffentliche)."""
        return self.stats.failed_pages - self.stats.gone_pages

    def _budget_left(self) -> bool:
        budget = self.config.max_detail_pages
        return budget is None or self.stats.detail_pages_attempted < budget

    def _failure(self, page: str) -> None:
        self.stats.parse_failures += 1
        metrics.record_scraper_parse_failure(self.fetcher.source_name, page)

    # -------------------- Körperschaft --------------------

    def build_body(self) -> dict[str, Any]:
        """Body der Quelle: eine Körperschaft je Basis-URL."""
        return {
            "id": self.urls.body_id(),
            "type": f"{OPARL}Body",
            "name": self.config.body_name,
            "shortName": self.config.body_name,
            "website": self.config.base_url,
        }

    async def resolve_bodies(self) -> list[dict[str, Any]]:
        return [self.build_body()]

    # -------------------- Crawl --------------------

    async def iter_entities(self, window: CrawlWindow, full: bool) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        """Wie ``iter_body_entities``, ohne Body-Kennung."""
        async for _body_id, entity_type, page in self.iter_body_entities(window, full):
            yield entity_type, page

    async def iter_body_entities(
        self, window: CrawlWindow, full: bool
    ) -> AsyncIterator[tuple[str, str, list[dict[str, Any]]]]:
        """Yield (Body-Kennung, entity_type, Seite) in FK-Reihenfolge."""
        body_id = self.urls.body_id()
        fetched_papers: set[int] = set()
        with_members = full or not self.config.members_on_full_only

        async for entity_type, page in self._factions(body_id, with_members):
            yield body_id, entity_type, page

        for year, month in window.months():
            if not self._budget_left():
                console.print(
                    f"[yellow]Scraper: Detailseiten-Budget erreicht ({self.config.max_detail_pages}) — "
                    f"Crawl endet vor {month:02d}/{year}[/yellow]"
                )
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "meeting", "paper", "consultation")
                break
            async for entity_type, page in self._month(body_id, year, month, full, with_members, fetched_papers):
                yield body_id, entity_type, page

        async for page in self._paper_list(body_id, full, fetched_papers):
            yield body_id, "paper", page

    @staticmethod
    def is_removable_agenda_item_id(external_id: str) -> bool:
        """Darf der Löschabgleich der Tagesordnung diesen Punkt markieren? Nur Punkte mit eigener Nummer (TOLFDNR)."""
        return "to020.asp?" in external_id and "TOLFDNR=" in external_id

    # -------------------- Fraktionen, Gremien, Mitglieder --------------------

    def _organization(
        self, body_id: str, page: str, param: str, number: int, name: str, org_type: str, classification: str
    ) -> dict[str, Any]:
        return with_content_hash(
            {
                "id": self.urls.external_id(page, **{param: number}),
                "type": f"{OPARL}Organization",
                "body": body_id,
                "name": name,
                "organizationType": org_type,
                "classification": classification,
                "web": self.urls.page(page, **{param: number}),
                "modified": self._crawl_time,
            }
        )

    async def _factions(self, body_id: str, with_members: bool) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        html = await self._fetch(self.urls.page("fr010"), "fr010")
        if html is None:
            self._failure("fr010")
            self.stats.mark_incomplete("Fraktionsliste nicht lesbar", "organization", "person", "membership")
            return
        factions = parse_factions(html)
        if not factions:
            console.print(f"[yellow]ALLRIS 3: Fraktionsliste ohne Fraktionen: {self.urls.page('fr010')}[/yellow]")
        orgs = [
            self._organization(body_id, "fr020", "FRLFDNR", f["frlfdnr"], f["name"], "Fraktion", "Fraktion")
            for f in factions
        ]
        self.stats.entities_parsed += len(orgs)
        if orgs:
            self._orgs_yielded.update(org["id"] for org in orgs)
            yield ("organization", orgs)
        if with_members:
            async for entity_type, page in self._members(body_id, orgs):
                yield entity_type, page

    async def _members(
        self, body_id: str, orgs: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        """Personen und Mitgliedschaften der Gremien bzw. Fraktionen (au020, pa020, fr020), je Lauf einmal."""
        persons: list[dict[str, Any]] = []
        memberships: list[dict[str, Any]] = []
        for org in orgs:
            if org["id"] in self._members_read:
                continue
            if not self._budget_left():
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "person", "membership")
                break
            self._members_read.add(org["id"])
            page = urlparse(org["id"]).path.rsplit("/", 1)[-1].removesuffix(".asp")
            html = await self._fetch(org["web"], page, is_detail=True)
            if html is None:
                if not self.last_not_public:
                    self._failure(page)
                    self.stats.mark_incomplete("Mitgliederliste nicht lesbar", "person", "membership")
                else:
                    self.stats.detail_pages_parsed += 1
                continue
            rows = parse_members(html)["members"]
            self.stats.detail_pages_parsed += 1
            for row, key in zip(rows, membership_keys(rows), strict=True):
                if row["kplfdnr"] is not None:
                    person_id = self.urls.external_id("kp020", KPLFDNR=row["kplfdnr"])
                else:
                    person_id = f"{body_id}#person/{_member_key(row)}"
                if person_id not in self._persons_yielded:
                    self._persons_yielded.add(person_id)
                    persons.append(
                        with_content_hash(
                            {
                                "id": person_id,
                                "type": f"{OPARL}Person",
                                "body": body_id,
                                "name": row["name"],
                                "modified": self._crawl_time,
                            }
                        )
                    )
                membership: dict[str, Any] = {
                    "id": f"{org['id']}#membership/{key}",
                    "type": f"{OPARL}Membership",
                    "person": person_id,
                    "organization": org["id"],
                    "votingRight": row["voting_right"],
                    "modified": self._crawl_time,
                }
                if row["role"]:
                    membership["role"] = row["role"]
                memberships.append(with_content_hash(membership))
            self.stats.entities_parsed += len(rows)
        if persons:
            yield ("person", persons)
        if memberships:
            yield ("membership", memberships)

    # -------------------- Kalendermonat --------------------

    async def _month(
        self,
        body_id: str,
        year: int,
        month: int,
        full: bool,
        with_members: bool,
        fetched_papers: set[int],
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        def calendar_of_month(page: str) -> bool:
            # Kalender mit den Tageszeilen, und zwar des angefragten Monats
            return is_calendar_page(page) and calendar_month(page) in (None, (year, month))

        html = await self._fetch(self.urls.page("si010_e", MM=month, YY=year), "si010", expected=calendar_of_month)
        if html is None:
            self._failure("si010")
            self.stats.mark_incomplete(f"Kalender {month:02d}/{year} nicht lesbar", "meeting", "paper", "consultation")
            return
        stubs = sorted(parse_calendar(html, year, month), key=MeetingStub.sort_key)
        month_key = f"{year:04d}-{month:02d}"
        snapshot = with_content_hash({"stubs": [asdict(stub) for stub in stubs]})["mandari:contentHash"]
        if not full and self.previous_snapshots.get(month_key) == snapshot:
            # Listen-Diffing: Monat unverändert, keine Detailseiten
            self.list_snapshots[month_key] = snapshot
            return

        # Den Monat erst als gelesen vermerken, wenn alle seine Seiten lesbar waren; sonst liest ihn der nächste
        # Änderungsabgleich wieder
        disturbances = self._disturbances()
        month_complete = True
        results: list[_MeetingResult] = []
        for stub in stubs:
            if not self._budget_left():
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "meeting", "paper", "consultation")
                month_complete = False
                break
            result = await self._build_meeting(stub, body_id)
            if isinstance(result, _Kept):
                # Tagesordnung nicht öffentlich: Es gibt die Sitzung, der Lauf schreibt sie nicht neu
                self.stats.mark_listed("meeting", self.urls.external_id("to010", SILFDNR=stub.silfdnr))
                continue
            if result is None:
                # Der Kalender nennt die Sitzung: Es gibt sie, nur ihre Seite war nicht lesbar
                self.stats.mark_listed("meeting", self.urls.external_id("to010", SILFDNR=stub.silfdnr))
                self.stats.mark_incomplete(f"Sitzung {stub.silfdnr} nicht lesbar", "paper", "consultation")
                continue
            results.append(result)

        committees = []
        for result in results:
            org = result.committee
            if org is not None and org["id"] not in self._orgs_yielded:
                self._orgs_yielded.add(org["id"])
                committees.append(org)
        if committees:
            self.stats.entities_parsed += len(committees)
            yield ("organization", committees)
            if with_members:
                async for entity_type, page in self._members(body_id, committees):
                    yield entity_type, page

        papers: list[dict[str, Any]] = []
        for result in results:
            for volfdnr in result.papers:
                if volfdnr in fetched_papers:
                    continue
                if not self._budget_left():
                    self.stats.mark_incomplete("Detailseiten-Budget erreicht", "paper")
                    month_complete = False
                    break
                fetched_papers.add(volfdnr)
                paper = await self._build_paper(volfdnr, body_id)
                if paper is not None:
                    papers.append(paper)
        if month_complete and self._disturbances() == disturbances:
            self.list_snapshots[month_key] = snapshot
        meetings = [result.meeting for result in results]
        consultations = [c for result in results for c in result.consultations]
        self.stats.entities_parsed += len(papers) + len(meetings) + len(consultations)
        if papers:
            yield ("paper", papers)
        if meetings:
            yield ("meeting", meetings)
        if consultations:
            yield ("consultation", consultations)

    # -------------------- Sitzung --------------------

    async def _build_meeting(self, stub: MeetingStub, body_id: str) -> _MeetingResult | _Kept | None:
        """
        Sitzung aus to010 (mit to020 je Punkt für vergangene Sitzungen), ohne veröffentlichte Tagesordnung aus dem
        Kalender. ``None``: nicht lesbar; ``_KEPT``: Tagesordnung nicht öffentlich. In beiden Fällen bleibt die Sitzung
        in diesem Lauf, wie sie ist: Aus dem Kalender allein entstünde sie ohne Gremium, Anschrift und Tagesordnung,
        und der Upsert nähme ihr eine bestehende Zuordnung zum Gremium.

        Eine angefangene Sitzung liest der Lauf zu Ende, auch wenn ihre Tagesordnungspunkte (to020) das Budget
        ``max_detail_pages`` überschreiten; vor jeder Sitzung wird es geprüft.
        """
        meeting_id = self.urls.external_id("to010", SILFDNR=stub.silfdnr)
        page = MeetingPage()
        if stub.agenda_linked:
            html = await self._fetch(self.urls.page("to010", SILFDNR=stub.silfdnr), "to010", is_detail=True)
            if html is None and self.last_not_public:
                self.stats.detail_pages_parsed += 1
                return _KEPT
            if html is None:
                self._failure("to010")
                return None
            page = parse_meeting(html)

        meeting: dict[str, Any] = {
            "id": meeting_id,
            "type": f"{OPARL}Meeting",
            "body": body_id,
            "name": page.title or stub.name,
            "modified": self._crawl_time,
        }
        if page.rows or page.title:
            meeting["web"] = self.urls.page("to010", SILFDNR=stub.silfdnr)
        day = page.date or stub.date
        start = _datetime(day, page.time_start or stub.time_start)
        if start:
            meeting["start"] = start
        end_time = page.time_end or stub.time_end
        end = _datetime(day, end_time) if end_time else None
        if end:
            meeting["end"] = end
        room = page.room or stub.room
        description = ", ".join(part for part in (room, page.address) if part)
        if description:
            location: dict[str, Any] = {
                "id": f"{body_id}#location/{_slug(description)}",
                "type": f"{OPARL}Location",
                "description": description,
            }
            if room:
                location["room"] = room
            if page.address:
                location["streetAddress"] = page.address
            meeting["location"] = location

        committee = None
        if page.committee is not None:
            kind, number, name = page.committee
            if kind == "au":
                committee = self._organization(body_id, "au020", "AULFDNR", number, name, "Gremium", "Ausschuss")
            else:
                committee = self._organization(body_id, "pa020", "PALFDNR", number, name, "Gremium", "Parlament")
            meeting["organization"] = [committee["id"]]
            meeting["mandari:committee"] = name

        # Protokoll und Beschluss stehen erst nach der Sitzung auf to020
        past = day is not None and day < self.today.isoformat()
        agenda_items: list[dict[str, Any]] = []
        consultations: list[dict[str, Any]] = []
        papers: list[int] = []
        for position, row in enumerate(page.rows, start=1):
            if row.tolfdnr is not None:
                item_id = self.urls.external_id("to020", TOLFDNR=row.tolfdnr)
            else:
                item_id = f"{meeting_id}#agendaitem/pos-{position}"
            item: dict[str, Any] = {
                "id": item_id,
                "type": f"{OPARL}AgendaItem",
                "meeting": meeting_id,
                "number": row.number,
                "order": position,
                "name": row.title,
                "public": row.public,
            }
            if row.decision:
                item["result"] = row.decision
            if row.detail and row.tolfdnr is not None:
                item["web"] = self.urls.page("to020", TOLFDNR=row.tolfdnr)
                if past:
                    details = await self._agenda_item_details(row.tolfdnr)
                    if details is None:
                        return None
                    if details.get("resolution"):
                        item["resolutionText"] = details["resolution"]
                    if details.get("minutes"):
                        item["mandari:minutes"] = details["minutes"]
            if row.volfdnr is not None:
                paper_id = self.urls.external_id("vo020", VOLFDNR=row.volfdnr)
                consultation_id = f"{paper_id}#consultation/{row.tolfdnr if row.tolfdnr is not None else position}"
                item["consultation"] = consultation_id
                consultations.append(
                    with_content_hash(
                        {
                            "id": consultation_id,
                            "type": f"{OPARL}Consultation",
                            "paper": paper_id,
                            "meeting": meeting_id,
                            "agendaItem": item_id,
                            "modified": self._crawl_time,
                        }
                    )
                )
                if row.volfdnr not in papers:
                    papers.append(row.volfdnr)
            agenda_items.append(item)
        if agenda_items:
            meeting["agendaItem"] = agenda_items
            # Gelesene Tagesordnung: Grundlage des Löschabgleich ihrer Punkte im Läufer (Bremse je Sitzung)
            self.stats.agendas[meeting_id] = {item["id"] for item in agenda_items}
        if stub.agenda_linked:
            self.stats.detail_pages_parsed += 1
        return _MeetingResult(
            meeting=with_content_hash(meeting), consultations=consultations, papers=papers, committee=committee
        )

    async def _agenda_item_details(self, tolfdnr: int) -> dict[str, str | None] | None:
        """
        Protokoll und Beschluss eines Punkts (to020). Nicht öffentlich oder toter Verweis: ohne Angaben.
        ``None``: Seite gestört, die Sitzung wird in diesem Lauf nicht geschrieben (sonst fehlten gespeicherte Texte).
        """
        html = await self._fetch(self.urls.page("to020", TOLFDNR=tolfdnr), "to020", is_detail=True)
        if html is not None:
            self.stats.detail_pages_parsed += 1
            return parse_agenda_item(html)
        if self.last_not_public or is_gone(getattr(self.fetcher, "last_status", None)):
            self.stats.detail_pages_parsed += 1
            return {}
        self._failure("to020")
        return None

    # -------------------- Drucksachen --------------------

    async def _build_paper(self, volfdnr: int, body_id: str) -> dict[str, Any] | None:
        paper_id = self.urls.external_id("vo020", VOLFDNR=volfdnr)
        html = await self._fetch(self.urls.page("vo020", VOLFDNR=volfdnr), "vo020", is_detail=True)
        if html is None and (self.last_not_public or is_gone(getattr(self.fetcher, "last_status", None))):
            # Nicht öffentlich bzw. keine Drucksache unter dieser Nummer: erwartete Antwort, kein Parse-Fehler
            self.stats.detail_pages_parsed += 1
            return None
        if html is None:
            self._failure("vo020")
            self.stats.mark_listed("paper", paper_id)
            return None
        parsed = parse_paper(html)
        if not parsed.get("name"):
            self._failure("vo020")
            return None
        self.stats.detail_pages_parsed += 1
        paper: dict[str, Any] = {
            "id": paper_id,
            "type": f"{OPARL}Paper",
            "body": body_id,
            "name": parsed["name"],
            "web": self.urls.page("vo020", VOLFDNR=volfdnr),
            "modified": self._crawl_time,
        }
        if parsed.get("reference"):
            paper["reference"] = parsed["reference"]
        if parsed.get("paper_type"):
            paper["paperType"] = parsed["paper_type"]
        if parsed.get("date"):
            paper["date"] = parsed["date"]
        if parsed.get("explanation"):
            paper["mandari:explanation"] = parsed["explanation"]
        if parsed.get("proposal"):
            paper["mandari:proposal"] = parsed["proposal"]
        return with_content_hash(paper)

    async def _paper_list(
        self, body_id: str, full: bool, fetched_papers: set[int]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Drucksachen der Liste vo040: im Änderungsabgleich nur neuere als beim letzten Lauf, im Vollabgleich alle
        der Liste, danach rückwärts bis ``scraper.oldest_paper_id`` (``_backfill``). Die neueste Kennung rückt nur vor,
        wenn alle gelesen sind (Budget, Störungen).
        """
        html = await self._fetch(self.urls.page("vo040"), "vo040")
        if html is None:
            self._failure("vo040")
            self.stats.mark_incomplete("Drucksachenliste nicht lesbar", "paper")
            return
        rows = parse_paper_list(html)
        if not rows:
            self._failure("vo040")
            self.stats.mark_incomplete("Drucksachenliste ohne Drucksachen", "paper")
            return
        newest = max(row["volfdnr"] for row in rows)
        previous_head = str(self.previous_snapshots.get(LIST_HEAD_KEY) or "")
        previous = int(previous_head) if previous_head.isdigit() and not full else 0
        wanted = sorted({row["volfdnr"] for row in rows if row["volfdnr"] > previous}, reverse=True)
        complete = True
        disturbances = self._disturbances()
        page: list[dict[str, Any]] = []
        for volfdnr in wanted:
            if volfdnr in fetched_papers:
                continue
            if not self._budget_left():
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "paper")
                complete = False
                break
            fetched_papers.add(volfdnr)
            paper = await self._build_paper(volfdnr, body_id)
            if paper is None:
                continue
            page.append(paper)
            self.stats.entities_parsed += 1
        if page:
            yield page
        if complete and self._disturbances() == disturbances:
            # Sonst bleibt die bisherige Kennung stehen (der Läufer übernimmt nur, was der Adapter meldet), und der
            # nächste Lauf liest die Drucksachen wieder
            self.list_snapshots[LIST_HEAD_KEY] = str(newest)
        if full and self.oldest_paper_id is not None:
            async for backfill_page in self._backfill(body_id, newest, fetched_papers):
                yield backfill_page

    async def _backfill(
        self, body_id: str, newest: int, fetched_papers: set[int]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Rückwärtszählen im Vollabgleich: Kennungen von der gemerkten Stelle (sonst der neuesten) abwärts bis
        ``scraper.oldest_paper_id``, je Lauf höchstens ``scraper.paper_backfill_per_run`` Abrufe (Standard
        ``PAPER_BACKFILL_PER_RUN``) und nie über ``max_detail_pages``.

        Die nächste Kennung steht in ``list_snapshots["vo020_backfill"]``; der Läufer speichert sie im Zustand der
        Quelle, spätere Vollabgleiche setzen dort fort. Sie rückt erst vor, wenn der Läufer die Drucksachen davor
        gespeichert hat. Unter der Untergrenze ist das Rückwärtszählen fertig; eine tiefere Untergrenze setzt es fort.
        Lücken (nicht öffentlich, toter Verweis) überspringt es. Bei einer gestörten Kennung hält es an, nach ihr:
        Ein gestörter Server kostet so je Lauf eine Kennung, nicht das ganze Kontingent.
        """
        lowest = self.oldest_paper_id
        if lowest is None:
            return
        stored = str(self.stored_snapshots.get(BACKFILL_KEY) or "")
        cursor = min(int(stored), newest) if stored.isdigit() else newest
        if cursor < lowest:
            return
        reads = 0
        page: list[dict[str, Any]] = []
        while cursor >= lowest:
            if cursor in fetched_papers:
                cursor -= 1
                continue
            if reads >= self.paper_backfill_per_run:
                break
            if not self._budget_left():
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "paper")
                break
            volfdnr = cursor
            fetched_papers.add(volfdnr)
            reads += 1
            disturbances = self._disturbances()
            paper = await self._build_paper(volfdnr, body_id)
            cursor = volfdnr - 1
            if paper is not None:
                page.append(paper)
                self.stats.entities_parsed += 1
            if self._disturbances() != disturbances:
                break
            if len(page) >= PAPER_PAGE_SIZE:
                yield page
                page = []
                # Der Läufer hat die Seite gespeichert: Stand fortschreiben
                self.list_snapshots[BACKFILL_KEY] = str(cursor)
        if page:
            yield page
        self.list_snapshots[BACKFILL_KEY] = str(cursor)
        console.print(
            f"[dim]ALLRIS 3: Rückwärtszählen {reads} Kennung(en), weiter bei {cursor} (Untergrenze {lowest})[/dim]"
        )
