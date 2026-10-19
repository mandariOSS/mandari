"""
SessionNet-Adapter (Somacos Session/SessionNet, Bürgerinfo-Frontend).

Verifiziert gegen SessionNet 5.5 (Layout 6) an zwei realen Instanzen:
- buergerinfo.luedenscheid.de (klassisch, *.asp)
- rat.eschweiler.de/bi (PHP-Variante, *.php — identisches Markup)
Geprüft außerdem an zwei weiteren Instanzen (5.4.5 und 5.5.0, *.php), Lücken dort siehe Issue #1075.

Seitenkürzel (produktweit stabil):
- si0040   Sitzungskalender (Monatsansicht, __cjahr/__cmonat/__canz)
- si0050   Sitzungsdetail "Informationen" (Gremium, Datum, Zeit, Raum, Dokumente)
- si0057   Sitzungsdetail "Tagesordnung" (TOPs Ö/NÖ, Beschlüsse, Vorlagen-Links)
- vo0050   Vorlagendetail (__kvonr; Betreff, Nummer, Art, Anlagen-PDFs)
- gr0040   Gremienliste
- kp0040   Gremium-Mitglieder (__kgrnr; Personen mit pe0051-Links)
- getfile  Datei-Download (id, type=do)

Filterleiste (gr0040, si0040, kp0040):
- __cwpnr  Wahlperiode (-> OParl LegislativeTerm am Body)
- __cpanr  Mandant = Körperschaft. Eine Instanz kann mehrere führen (z. B. eine
           Samtgemeinde mit ihren Mitgliedsgemeinden); mit scraper.bodies wird
           je Mandant ein eigener Body gecrawlt.

external_id = normalisierte kanonische Detail-URL (nur ID-Parameter, sortiert).
Tagesordnungspunkte haben keine eigene Seite: ``<Sitzung>#agendaitem/<Teil>``, der Teil kommt aus der
Vorlage (``kvonr-<__kvonr>``), sonst aus der Nummer (``nr-…``) oder der Position (``pos-…``), siehe
``agenda_item_keys`` (Issue #863).
Ausgabe: synthetische OParl-1.1-Dicts für die bestehende Pipeline.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from mandari_oparl.abgleich import detect_gate, is_gone

from src.metrics import metrics
from src.redaction import MaskingConsole
from src.scrapers.base import (
    BodySpec,
    CrawlWindow,
    ScraperConfig,
    ScrapeStats,
    normalize_external_id,
    with_content_hash,
)
from src.scrapers.politeness import PoliteFetcher

console = MaskingConsole()

OPARL = "https://schema.oparl.org/1.1/"
TZ_BERLIN = ZoneInfo("Europe/Berlin")

_TIME_RANGE_RE = re.compile(r"^(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?(?:\s*Uhr)?$")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
#: Jede Seite einer SessionNet-Instanz trägt deren Layout (Klassen ``smc…``), und die Parser lesen nur
#: dieses. Fehlt es, kam eine fremde Seite zurück: eine Sperr-, Prüf- oder Hinweisseite (Issue #556).
#: Der Produktname allein genügt nicht: Prüfseiten übernehmen die angefragte Adresse (üblicher
#: Installationspfad ``/sessionnet/…``) in Formular, Weiterleitung oder Titel.
_SMC_LAYOUT_RE = re.compile(r"class=[\"'][^\"']*\bsmc", re.IGNORECASE)
#: Fehlerseite von SessionNet im eigenen Layout, mit Status 200 (Issue #1075): ``smc_page_error_layout`` am Rahmen,
#: Titel "SessionNet Fehlermeldung"
_ERROR_PAGE_RE = re.compile(r"class=[\"'][^\"']*\bsmc_page_error_layout\b|<title>[^<]*Fehlermeldung", re.IGNORECASE)
_ERROR_CODE_RE = re.compile(r"Fehlercode:\s*(\d+)")
#: "Zum Öffnen des Vorgangs fehlt die Berechtigung": Der Vorgang ist nicht öffentlich
ERROR_CODE_NOT_PUBLIC = "1104"


def is_sessionnet_page(html: str) -> bool:
    """Trägt die Seite das SessionNet-Layout (und ist damit keine Sperr-, Prüf- oder Hinweisseite)?"""
    return _SMC_LAYOUT_RE.search(html) is not None


def sessionnet_error_code(html: str) -> str | None:
    """
    Fehlercode, wenn die Seite die Fehlerseite von SessionNet ist ("" ohne Code), sonst ``None``.

    SessionNet beantwortet etwa einen nicht öffentlichen Vorgang mit Status 200 und einer Fehlerseite im eigenen
    Layout ("Zum Öffnen des Vorgangs fehlt die Berechtigung. Fehlercode: 1104"). Ausgewertet ergäbe sie eine Vorlage
    namens "SessionNet Fehlermeldung" (Issue #1075).
    """
    if _ERROR_PAGE_RE.search(html) is None:
        return None
    match = _ERROR_CODE_RE.search(html)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# URL-Schema
# ---------------------------------------------------------------------------


@dataclass
class SessionNetUrls:
    """Baut Seiten-URLs und kanonische external_ids einer Instanz."""

    base_url: str  # mit Slash am Ende, z. B. https://rat.eschweiler.de/bi/
    ext: str = "asp"  # "asp" | "php"

    def page(self, name: str, **params: Any) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base_url}{name}.{self.ext}"
        return f"{url}?{query}" if query else url

    def external_id(self, name: str, **params: Any) -> str:
        return normalize_external_id(
            self.page(name, **params),
            keep_params=("__ksinr", "__kvonr", "__kgrnr", "__kpenr", "__cpanr", "__cwpnr", "id", "type"),
        )

    def body_id(self, cpanr: int | None = None) -> str:
        """Body-Kennung: Basis-URL, bei mehreren Körperschaften mit ``__cpanr``."""
        if cpanr is None:
            return normalize_external_id(self.base_url, keep_params=())
        return normalize_external_id(f"{self.base_url}?__cpanr={cpanr}", keep_params=("__cpanr",))


def _query_int(href: str, param: str) -> int | None:
    """Extrahiert einen Integer-Query-Parameter aus einem (relativen) Link."""
    try:
        values = parse_qs(urlparse(href).query).get(param)
        return int(values[0]) if values else None
    except (ValueError, TypeError):
        return None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Parser (pure Funktionen, golden-file-getestet)
# ---------------------------------------------------------------------------


@dataclass
class MeetingStub:
    """Kalender-Eintrag: Basisdaten einer Sitzung aus si0040."""

    ksinr: int
    name: str
    committee: str | None = None
    date: str | None = None  # dd.mm.yyyy
    time_start: str | None = None  # HH:MM
    time_end: str | None = None
    location: str | None = None
    #: Verweist der Eintrag auf die Tagesordnung (si0057)? Angekündigte Sitzungen ohne veröffentlichte
    #: Tagesordnung nennen ihre Nummer nur im Termin-Link (Issue #1075).
    agenda_linked: bool = True

    def sort_key(self) -> tuple:
        return (self.date or "", self.ksinr)


def is_calendar_page(html: str) -> bool:
    """
    Trägt die Seite den Monatskalender (si0040)? Er hat je Tag eine Zelle ``td.silink``, auch an Tagen
    ohne Sitzung; ein Monat ohne Sitzungen hat sie also ebenfalls. Fehlen sie, ist die Seite nicht lesbar
    (etwa eine Hinweis- oder Prüfseite im SessionNet-Layout) und ergibt keine leere Liste (Issue #556).
    """
    return BeautifulSoup(html, "html.parser").select_one("td.silink") is not None


#: Kennung im Termin-Link (yvcs, "Termin übernehmen"): ``uid=si<__ksinr>``
_TERMIN_UID_RE = re.compile(r"^si(\d+)$")


def _announced_stub(cell: Any) -> MeetingStub | None:
    """
    Kalendereintrag ohne Link auf die Tagesordnung (Issue #1075).

    Solange die Tagesordnung nicht veröffentlicht ist, nennt der Kalender die Sitzung nur mit Gremium, Zeit und Ort;
    ihre Nummer steht im Link "Termin übernehmen" (``yvcs….php?key=<__ksinr>&uid=si<__ksinr>&…``). Ohne diesen Link
    (etwa vergangene nichtöffentliche Sitzungen) hat der Eintrag keine stabile Kennung und wird übersprungen.
    """
    row = cell.find_parent("tr")
    termin = row.find("a", href=re.compile(r"yvcs\.(asp|php)\?")) if row is not None else None
    if termin is None:
        return None
    href = _attr(termin, "href")
    uid = _TERMIN_UID_RE.match(parse_qs(urlparse(href).query).get("uid", [""])[0])
    ksinr = _query_int(href, "key")
    if uid is None or ksinr is None or int(uid.group(1)) != ksinr:
        return None
    heading = cell.find("div", class_="smc-el-h")
    name = _clean(heading.get_text() if heading is not None else "")
    if not name:
        return None
    stub = MeetingStub(ksinr=ksinr, name=name, committee=name, agenda_linked=False)
    # title="Termin übernehmen: <dd.mm.yyyy> <Gremium>"
    date_match = _DATE_RE.search(_clean(_attr(termin, "title")) or _clean(_attr(termin, "aria-label")))
    if date_match:
        stub.date = date_match.group(1)
    return stub


def parse_calendar(html: str) -> list[MeetingStub]:
    """Parst die Monatsansicht des Sitzungskalenders (si0040)."""
    soup = BeautifulSoup(html, "html.parser")
    stubs: list[MeetingStub] = []
    for cell in soup.select("td.silink"):
        link = cell.find("a", href=re.compile(r"si0057\.(asp|php)\?"))
        if link is None:
            announced = _announced_stub(cell)
            if announced is None:
                continue
            stub = announced
        else:
            ksinr = _query_int(link.get("href", ""), "__ksinr")
            if ksinr is None:
                continue
            stub = MeetingStub(ksinr=ksinr, name=_clean(link.get_text()))
            # title="Details anzeigen: <Gremium> <dd.mm.yyyy>"
            title = _clean(link.get("title", ""))
            if title.startswith("Details anzeigen:"):
                rest = title.removeprefix("Details anzeigen:").strip()
                date_match = _DATE_RE.search(rest)
                if date_match:
                    stub.date = date_match.group(1)
                    stub.committee = _clean(rest[: date_match.start()]) or None
        # <ul class="smc-detail-list"><li>17:00-18:45 Uhr</li><li>Ort</li></ul>
        detail_list = cell.find("ul", class_="smc-detail-list")
        if detail_list is not None:
            for li in detail_list.find_all("li"):
                text = _clean(li.get_text())
                time_match = _TIME_RANGE_RE.match(text.replace("\xa0", " ").strip())
                if time_match:
                    stub.time_start = time_match.group(1)
                    stub.time_end = time_match.group(2)
                elif text and stub.location is None:
                    stub.location = text
        stubs.append(stub)
    return stubs


def _parse_info_table(soup: BeautifulSoup, field_classes: dict[str, str]) -> dict[str, str]:
    """Liest die Feld-Tabellen (div.smc-table-cell.<klasse>) von si0050/vo0050."""
    result: dict[str, str] = {}
    for key, css_class in field_classes.items():
        cell = soup.select_one(f"div.smc-table-cell.{css_class}")
        if cell is not None:
            value = _clean(cell.get_text())
            if value:
                result[key] = value
    return result


def _parse_file_links(root: Any) -> list[dict[str, str]]:
    """
    Sammelt getfile-Links (dedupliziert je href, Name bevorzugt aus
    Link-Text, sonst aus title-Attribut).
    """
    files: dict[str, dict[str, str]] = {}
    for link in root.find_all("a", href=re.compile(r"getfile\.(asp|php)\?")):
        href = link.get("href", "")
        name = _clean(link.get_text())
        title = _clean(link.get("title", ""))
        entry = files.setdefault(href, {"href": href, "name": "", "title": title})
        if name and not entry["name"]:
            entry["name"] = name
        if title and not entry["title"]:
            entry["title"] = title
    return list(files.values())


def parse_meeting_info(html: str) -> dict[str, Any]:
    """
    Parst den Informationen-Tab einer Sitzung (si0050).

    Liefert: name (Sitzungsnummer), committee, room, date, time, files.
    """
    soup = BeautifulSoup(html, "html.parser")
    info = _parse_info_table(
        soup,
        {
            "name": "siname",
            "committee": "sigrname",
            "room": "siort",
            "date": "sidat",
            "time": "yytime",
        },
    )
    content = soup.find(id="page-content") or soup
    info["files"] = _parse_file_links(content)
    return info


@dataclass
class AgendaRow:
    """Eine TOP-Zeile aus si0057."""

    number: str | None
    title: str
    public: bool = True
    result: str | None = None
    vote: str | None = None
    paper_kvonr: int | None = None
    paper_reference: str | None = None


@dataclass
class MeetingAgenda:
    title: str | None = None
    rows: list[AgendaRow] = field(default_factory=list)


def parse_meeting_agenda(html: str) -> MeetingAgenda:
    """Parst die Tagesordnung einer Sitzung (si0057) inkl. Ö/NÖ und Beschlüssen."""
    soup = BeautifulSoup(html, "html.parser")
    agenda = MeetingAgenda()
    h1 = soup.find("h1", class_="smc_h1")
    if h1 is not None:
        agenda.title = _clean(h1.get_text())

    table = soup.find("table", class_=re.compile(r"smctablesitzung\b")) or soup.find(
        "table", id=re.compile(r"si0057_contenttable")
    )
    if table is None:
        return agenda

    public = True
    for row in table.find_all("tr"):
        section = row.find("td", class_="totrenn")
        if section is not None:
            heading = _clean(section.get_text()).lower()
            if "nicht" in heading:  # "Nichtöffentlicher Teil:" / "Nicht öffentlicher ..."
                public = False
            elif "öffentlich" in heading:
                public = True
            continue

        num_cell = row.find("td", class_="tofnum")
        # Titelzelle: "tobetr" (Text) oder "tolink" (TOP mit Detail-Link)
        title_cell = row.find("td", class_=lambda c: c in ("tobetr", "tolink"))
        if num_cell is None or title_cell is None:
            continue

        badge = num_cell.find("span", class_="badge")
        raw_number = _clean(badge.get_text() if badge else num_cell.get_text()).replace("\xa0", " ")
        # "Ö 3.2" / "N 1" -> "3.2" / "1". Das Kennzeichen entscheidet, ob der Punkt öffentlich ist: Nicht jede
        # Tagesordnung hat Abschnittsüberschriften (Issue #1075); ohne Kennzeichen gilt die Sektion.
        marker = re.match(r"^([ÖöNn])\s+", raw_number)
        number = (raw_number[marker.end() :] if marker else raw_number).strip() or None

        title_div = title_cell.find("div", class_=re.compile(r"smc-card-header-title"))
        title = _clean(title_div.get_text() if title_div else title_cell.get_text())
        if not title:
            continue

        item = AgendaRow(number=number, title=title, public=public if marker is None else marker.group(1) in "Öö")

        beschluss = title_cell.find("p", class_=re.compile(r"box2_beschluss"))
        if beschluss is not None:
            item.result = _clean(beschluss.get_text()).removeprefix("Beschluss:").strip() or None
        abstimmung = title_cell.find("p", class_=re.compile(r"box2_abstimmung"))
        if abstimmung is not None:
            item.vote = _clean(abstimmung.get_text()).removeprefix("Abstimmung:").strip() or None

        vorlage = row.find("a", href=re.compile(r"vo0050\.(asp|php)\?"))
        if vorlage is not None:
            item.paper_kvonr = _query_int(vorlage.get("href", ""), "__kvonr")
            item.paper_reference = _clean(vorlage.get_text()) or None

        agenda.rows.append(item)
    return agenda


def parse_paper(html: str) -> dict[str, Any]:
    """Parst ein Vorlagendetail (vo0050): Betreff, Nummer, Art, Anlagen."""
    soup = BeautifulSoup(html, "html.parser")
    info = _parse_info_table(
        soup,
        {"name": "vobetr", "reference": "voname", "paper_type": "vovaname"},
    )
    if "name" not in info:
        h1 = soup.find("h1", class_="smc_h1")
        if h1 is not None:
            title = _clean(h1.get_text())
            if title:
                info["name"] = title
    content = soup.find(id="page-content") or soup
    info["files"] = _parse_file_links(content)
    return info


def parse_organizations(html: str) -> list[dict[str, Any]]:
    """
    Parst die Gremienliste (gr0040): kgrnr, Name und ob das Gremium eine Mitgliederseite (kp0040) hat.

    Gremien, deren Mitglieder nicht veröffentlicht sind (etwa Verwaltungs- oder Samtgemeindeausschuss), führt die
    Liste ohne Link auf kp0040; ihre Nummer steht dann nur im Link auf die Sitzungsliste (si0046, ``__ksigrnr``,
    dieselbe Nummer wie ``__kgrnr``). Ohne beide Links fehlt eine stabile Kennung: übersprungen (Issue #1075).
    """
    soup = BeautifulSoup(html, "html.parser")
    orgs: dict[int, dict[str, Any]] = {}
    for cell in soup.select("td.grname"):
        link = cell.find("a", href=re.compile(r"kp0040\.(asp|php)\?"))
        if link is not None:
            kgrnr = _query_int(link.get("href", ""), "__kgrnr")
            name = _clean(link.get_text())
        else:
            row = cell.find_parent("tr")
            meetings = row.find("a", href=re.compile(r"si0046\.(asp|php)\?.*__ksigrnr=")) if row is not None else None
            kgrnr = _query_int(_attr(meetings, "href"), "__ksigrnr") if meetings is not None else None
            name = _clean(cell.get_text())
        if kgrnr is None or not name:
            continue
        orgs.setdefault(kgrnr, {"kgrnr": kgrnr, "name": name, "members_page": link is not None})
    return list(orgs.values())


#: Abschnitte der Mitgliederliste ohne Stimmrecht: "… ohne Stimmrecht", "mit beratender Stimme", "Beratende
#: Mitglieder", "zur Beratung hinzugezogen", "Verwaltung" (Protokoll, Fachbereiche; Issue #1075)
_NO_VOTE_RE = re.compile(r"ohne stimmrecht|beratend|hinzugezogen|^verwaltung\b", re.IGNORECASE)


def _cell_date(row: Any, css_class: str) -> str | None:
    """Datum (ISO) aus der ersten Zelle der Klasse, etwa ``mgadat`` (von) oder ``mgedat`` (bis)."""
    cell = row.find("td", class_=css_class)
    match = _DATE_RE.search(_clean(cell.get_text())) if cell is not None else None
    return _iso_date(match.group(1)) if match else None


def _ended_table(table: Any) -> bool:
    """Steht die Tabelle unter der Überschrift "Mitgliedschaft beendet:"?"""
    heading = table.find_previous(["h2", "h3"]) if table is not None else None
    return heading is not None and "beendet" in _clean(heading.get_text()).lower()


def parse_members(html: str) -> list[dict[str, Any]]:
    """
    Parst die Mitgliederliste eines Gremiums (kp0040).

    Liefert Zeilen mit kpenr, Name, Partei/Mitgliedschaft, Funktion,
    Stimmrecht (aus der Abschnittsüberschrift, z. B. "... ohne Stimmrecht:")
    sowie Beginn und Ende der Mitgliedschaft (Spalten "von"/"bis", ISO).

    Unter "Mitgliedschaft beendet:" stehen frühere Mitglieder in einer eigenen
    Tabelle mit Beginn und Ende (Issue #1075). Sie bekommen ihr Ende; eine
    beendete Mitgliedschaft ohne Ende fehlt, statt als aktuelle zu erscheinen.
    """
    soup = BeautifulSoup(html, "html.parser")
    members: list[dict[str, Any]] = []
    section: str | None = None
    table: Any = None
    ended = False
    for row in soup.find_all("tr"):
        row_table = row.find_parent("table")
        if row_table is not table:
            # Jede Tabelle beginnt ohne Abschnitt; die Tabelle der beendeten Mitgliedschaften hat oft keinen
            table, section, ended = row_table, None, _ended_table(row_table)
        header = row.find("td", class_=re.compile(r"smcfield_puname"))
        if header is not None:
            section = _clean(header.get_text()).rstrip(":") or None
            continue
        name_cell = row.find("td", class_="pename")
        if name_cell is None:
            continue
        link = name_cell.find("a", href=re.compile(r"pe0051\.(asp|php)\?"))
        if link is not None:
            kpenr = _query_int(link.get("href", ""), "__kpenr")
            name = _clean(link.get_text())
        else:
            # Personen ohne Detailseite (z. B. sachkundige Bürger:innen)
            # erscheinen ohne pe0051-Link — nur mit Namen aufnehmen.
            kpenr = None
            name = _clean(name_cell.get_text())
        if not name:
            continue
        end_date = _cell_date(row, "mgedat")
        if ended and end_date is None:
            continue
        party_cell = row.find("td", class_="pepartei")
        role_cell = row.find("td", class_="amname")
        members.append(
            {
                "kpenr": kpenr,
                "name": name,
                "party": _clean(party_cell.get_text()) if party_cell else None,
                "role": (_clean(role_cell.get_text()) if role_cell else None) or section,
                "section": section,
                "voting_right": not (section and _NO_VOTE_RE.search(section)),
                "start_date": _cell_date(row, "mgadat"),
                "end_date": end_date,
            }
        )
    return members


# Mandantenauswahl: "Alle Mandanten" ist eine Ansicht, keine Körperschaft
_ALL_VIEW_RE = re.compile(r"^alle\b", re.IGNORECASE)
_TERM_WORD_RE = re.compile(r"wahlperiode", re.IGNORECASE)
# Sammelansichten der Wahlperioden-Auswahl ("Alle Wahlperioden", "Gesamt ab 2004")
_ALL_TERMS_RE = re.compile(r"^(alle|gesamt)\b", re.IGNORECASE)


def _attr(tag: Any, name: str) -> str:
    """Attributwert als Text (bs4 liefert bei Mehrfachwerten Listen)."""
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return str(value or "")


def _mandant_toggle(soup: BeautifulSoup) -> Any:
    """Schalter der Mandantenauswahl in der Filterleiste (Beschriftung = gewählter Mandant)."""
    for toggle in soup.find_all("a", class_="dropdown-toggle"):
        label = f"{_attr(toggle, 'aria-label')} {_attr(toggle, 'title')}".lower()
        if "mandant" in label:
            return toggle
    return None


def parse_mandanten(html: str) -> list[dict[str, Any]]:
    """
    Parst die Mandantenauswahl der Filterleiste (gr0040, si0040).

    Instanzen mit mehreren Körperschaften (z. B. eine Samtgemeinde mit ihren
    Mitgliedsgemeinden) zeigen den gewählten Mandanten als Beschriftung und die
    übrigen als Links mit ``__cpanr``. Liefert ``{"cpanr", "name", "selected"}``;
    der gewählte Mandant steht vorn und hat ``cpanr`` None (die Seite nennt seine
    Nummer nicht). "Alle Mandanten" ist keine Körperschaft und fehlt.
    Instanzen ohne Mandantenauswahl: leere Liste.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict[str, Any]] = []
    toggle = _mandant_toggle(soup)
    if toggle is not None:
        name = _clean(toggle.get_text())
        if name and not _ALL_VIEW_RE.match(name):
            result.append({"cpanr": None, "name": name, "selected": True})
    seen: set[int] = set()
    for link in soup.select("a.smcfiltermenumandant"):
        cpanr = _query_int(_attr(link, "href"), "__cpanr")
        name = _clean(link.get_text())
        if cpanr is None or cpanr < 1 or cpanr in seen or not name or _ALL_VIEW_RE.match(name):
            continue
        seen.add(cpanr)
        result.append({"cpanr": cpanr, "name": name, "selected": False})
    return result


def parse_calendar_mandanten(html: str) -> dict[int, str]:
    """
    Mandant je Kalendereintrag (si0040, Spalte "Mandant" = ``td.pagel``).

    Nur Instanzen mit mehreren Körperschaften haben die Spalte; sonst leer.
    Dient als Gegenprobe, dass die Instanz den Kalender wirklich nach
    ``__cpanr`` gefiltert hat.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[int, str] = {}
    for cell in soup.select("td.silink"):
        link = cell.find("a", href=re.compile(r"si0057\.(asp|php)\?"))
        if link is None:
            continue
        ksinr = _query_int(_attr(link, "href"), "__ksinr")
        row = cell.find_parent("tr")
        mandant_cell = row.find("td", class_="pagel") if row is not None else None
        if ksinr is None or mandant_cell is None:
            continue
        name = _clean(mandant_cell.get_text())
        if name:
            result[ksinr] = name
    return result


def _iso_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def parse_legislative_terms(html: str) -> list[dict[str, Any]]:
    """
    Parst die Wahlperioden aus der Filterleiste (gr0040, kp0040; ``__cwpnr``).

    Liefert ``{"wpnr", "name", "start_date", "end_date", "selected"}`` (Datum ISO).
    Sammelansichten ("Alle Wahlperioden", "Gesamt ab …", "Alle Daten") fehlen; Sortierlinks
    mit ``__cwpnr`` gehören nicht zur Auswahl. Fehlt im Text das Wort
    "Wahlperiode", entsteht der Name aus den Jahren ("Wahlperiode 2021–2026").
    """
    soup = BeautifulSoup(html, "html.parser")
    terms: list[dict[str, Any]] = []
    seen: set[int] = set()
    for link in soup.find_all("a", href=re.compile(r"__cwpnr=")):
        classes = _attr(link, "class").split()
        if not any(css.startswith("smcfiltermenu") for css in classes):
            continue
        wpnr = _query_int(_attr(link, "href"), "__cwpnr")
        label = _clean(link.get_text())
        if wpnr is None or wpnr in seen or not label or _ALL_TERMS_RE.match(label):
            continue
        seen.add(wpnr)
        dates = _DATE_RE.findall(_clean(_attr(link, "title"))) or _DATE_RE.findall(label)
        start = _iso_date(dates[0]) if dates else None
        end = _iso_date(dates[1]) if len(dates) > 1 else None
        if _TERM_WORD_RE.search(label) or start is None:
            name = label
        elif end is not None:
            name = f"Wahlperiode {start[:4]}–{end[:4]}"
        else:
            name = f"Wahlperiode ab {start[:4]}"
        terms.append(
            {
                "wpnr": wpnr,
                "name": name,
                "start_date": start,
                "end_date": end,
                "selected": "smcfiltermenuselected" in classes,
            }
        )
    return terms


# ---------------------------------------------------------------------------
# Kennungen der Tagesordnungspunkte (Issue #863)
# ---------------------------------------------------------------------------

#: Anfänge der Kennungsteile seit Issue #863. Bis dahin war der Teil die Nummer selbst (``3.2``, ``N1``, ``3-7``).
AGENDA_ITEM_KEY_PREFIXES = ("kvonr-", "nr-", "pos-")
_AGENDA_ITEM_FRAGMENT = "#agendaitem/"


def agenda_item_key(row: AgendaRow, position: int) -> str:
    """
    Kennungsteil eines Tagesordnungspunkts hinter ``#agendaitem/``, ohne Zählung doppelter Teile.

    Die Sitzungsseite (si0057) nennt je Punkt keine eigene Kennung und keinen Link auf eine Detailseite. Stabil ist
    nur die Vorlage: Ein Punkt mit Vorlage heißt nach ihr (``kvonr-<__kvonr>``), gleich unter welcher Nummer und in
    welchem Teil der Sitzung er steht. Punkte ohne Vorlage (Eröffnung, Mitteilungen, Anfragen, Gliederungspunkte)
    haben keine stabile Quelle der Kennung; sie heißen nach ihrer Nummer, im nichtöffentlichen Teil mit ``N`` wie
    auf der Seite (``nr-3.2``, ``nr-N1``), ohne Nummer nach ihrer Position (``pos-7``). Fügt die Verwaltung davor
    einen Punkt ein, trägt ihre Kennung danach einen anderen Punkt; Work fängt das über den fachlichen Anker ab
    (#547).
    """
    if row.paper_kvonr is not None:
        return f"kvonr-{row.paper_kvonr}"
    if row.number:
        return f"nr-{row.number}" if row.public else f"nr-N{row.number}"
    return f"pos-{position}"


def agenda_item_keys(rows: Sequence[AgendaRow]) -> list[str]:
    """
    Kennungsteile aller Tagesordnungspunkte einer Sitzung (``agenda_item_key``) in der Reihenfolge der Seite.

    Steht derselbe Teil zweimal auf der Seite (dieselbe Vorlage an zwei Punkten, eine Nummer doppelt), zählt der
    spätere weiter: ``kvonr-12770-2``.
    """
    used: set[str] = set()
    keys: list[str] = []
    for position, row in enumerate(rows, start=1):
        base = agenda_item_key(row, position)
        key, count = base, 1
        while key in used:
            count += 1
            key = f"{base}-{count}"
        used.add(key)
        keys.append(key)
    return keys


def legacy_agenda_item_keys(rows: Sequence[AgendaRow]) -> list[str]:
    """
    Kennungsteile, wie der Adapter sie bis Issue #863 vergab: die Nummer, bei einer Wiederholung im
    nichtöffentlichen Teil mit ``N``, sonst mit der Position. Nur noch, um den Bestand umzustellen.
    """
    used: set[str] = set()
    keys: list[str] = []
    for position, row in enumerate(rows, start=1):
        key = row.number or str(position)
        legacy = key
        if legacy in used:
            legacy = f"{'' if row.public else 'N'}{key}"
            if legacy in used:
                legacy = f"{key}-{position}"
        used.add(legacy)
        keys.append(legacy)
    return keys


def is_legacy_agenda_item_id(external_id: str) -> bool:
    """Hat die Kennung eines Tagesordnungspunkts die Form von vor Issue #863 (Teil = Nummer)?"""
    _, fragment, key = external_id.partition(_AGENDA_ITEM_FRAGMENT)
    return bool(fragment) and bool(key) and not key.startswith(AGENDA_ITEM_KEY_PREFIXES)


@dataclass(frozen=True)
class AgendaItemKey:
    """Kennung eines Tagesordnungspunkts im laufenden Abruf und die Angaben, um seine bisherige Zeile zu finden."""

    item_id: str
    #: Kennung, die der Adapter bis Issue #863 für diesen Punkt vergeben hätte
    legacy_id: str
    name: str
    public: bool
    #: Beratung der Vorlage in dieser Sitzung (``…vo0050…#consultation/<ksinr>``), ``None`` ohne Vorlage
    consultation: str | None


def _stored_order(row: Mapping[str, Any]) -> tuple[bool, int, str]:
    """Reihenfolge der Zeilen im Bestand: nach ``order``, Zeilen ohne Angabe zuletzt."""
    return (row.get("order") is None, int(row.get("order") or 0), str(row["external_id"]))


def _own_key(row: Mapping[str, Any]) -> str:
    """Kennungsteil, den eine Zeile im Bestand nach ihren eigenen Angaben in der neuen Form trüge."""
    consultation = row.get("consultation")
    kvonr = _query_int(str(consultation), "__kvonr") if consultation else None
    number = str(row.get("number") or "").strip() or None
    agenda_row = AgendaRow(number=number, title="", public=row.get("public") is not False, paper_kvonr=kvonr)
    return agenda_item_key(agenda_row, int(row.get("order") or 0))


def plan_agenda_item_renames(keys: Sequence[AgendaItemKey], stored: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """
    Neue Kennungen für die Zeilen einer Sitzung, die noch die Form von vor Issue #863 tragen.

    ``keys``: die Punkte der Sitzung im laufenden Abruf. ``stored``: ihre Punkte im Bestand mit ``external_id``,
    ``number``, ``name``, ``public``, ``order`` und ``consultation`` (aus ``raw_json``). Ergebnis: bisherige Kennung
    -> neue. Der Bestand schreibt sie in place um, ``id`` bleibt; Notizen, Positionen und Verweise bleiben an ihrer
    Zeile.

    1. Punkt mit Vorlage: die bisherige Zeile mit derselben Beratung, auch wenn die Nummer sich inzwischen geändert
       hat. Berät die Sitzung die Vorlage an mehreren Punkten, paarweise in der Reihenfolge, wenn es gleich viele
       sind; sonst nur die Zeile unter der bisherigen Kennung, sofern sie dieselbe Beratung trägt.
    2. Punkt ohne Vorlage: die Zeile ohne Vorlage mit demselben Namen und gleich öffentlich, wenn der Name auf
       beiden Seiten nur einmal vorkommt (so findet ein umnummerierter Punkt seine Zeile); sonst die Zeile unter der
       bisherigen Kennung (Nummer), wenn auch sie keine Vorlage hatte und gleich öffentlich ist – dieselbe Zeile, die
       der Abruf bisher überschrieben hätte. Wechselt dabei der Inhalt, fängt Work das über den fachlichen Anker ab
       (#547).
    3. Übrige Zeilen in der bisherigen Form (der Punkt steht nicht mehr auf der Tagesordnung): die Kennung, die sie
       nach ihren eigenen Angaben trügen (``_own_key``), sofern frei, sonst mit Zählung (``-2``). So bleibt in
       einer abgerufenen Sitzung keine Zeile in der bisherigen Form; der Löschabgleich der Tagesordnung behandelt
       sie wie jeden Punkt, der nicht mehr geliefert wird.

    Zeilen in der neuen Form fasst die Umstellung nie an. Eine Kennung in der bisherigen Form vergibt der Adapter nie
    mehr; die behaltene ``id`` einer umgeschriebenen Zeile (``uuid5`` der bisherigen Kennung) stößt deshalb mit keiner
    neuen Zeile zusammen.
    """
    in_store = list(stored)
    rows = [row for row in in_store if is_legacy_agenda_item_id(str(row.get("external_id") or ""))]
    if not rows:
        return {}
    existing = {str(row.get("external_id")) for row in in_store}
    pending = [key for key in keys if key.item_id not in existing]
    by_id = {str(row["external_id"]): row for row in rows}
    renames: dict[str, str] = {}

    consultations = list(dict.fromkeys(key.consultation for key in pending if key.consultation))
    for consultation in consultations:
        items = [key for key in pending if key.consultation == consultation]
        candidates = sorted((row for row in rows if row.get("consultation") == consultation), key=_stored_order)
        if len(candidates) == len(items):
            pairs = [(str(row["external_id"]), key) for row, key in zip(candidates, items, strict=True)]
        else:
            pairs = [
                (key.legacy_id, key)
                for key in items
                if key.legacy_id in by_id and by_id[key.legacy_id].get("consultation") == consultation
            ]
        for legacy_id, key in pairs:
            renames[legacy_id] = key.item_id

    # Ohne Vorlage: zuerst über den Namen, wenn er auf beiden Seiten nur einmal vorkommt, dann über die Nummer
    def name_key(name: Any, public: bool) -> tuple[str, bool]:
        return (_clean(str(name or "")).casefold(), public)

    ohne = [key for key in pending if not key.consultation]
    frei = [row for row in rows if not row.get("consultation") and str(row["external_id"]) not in renames]
    nach_name: dict[tuple[str, bool], list[AgendaItemKey]] = {}
    for key in ohne:
        if key.name:
            nach_name.setdefault(name_key(key.name, key.public), []).append(key)
    zeilen_nach_name: dict[tuple[str, bool], list[Mapping[str, Any]]] = {}
    for row in frei:
        if row.get("name"):
            zeilen_nach_name.setdefault(name_key(row["name"], row.get("public") is not False), []).append(row)
    for name, items in nach_name.items():
        kandidaten = zeilen_nach_name.get(name, [])
        if len(items) == 1 and len(kandidaten) == 1:
            renames[str(kandidaten[0]["external_id"])] = items[0].item_id
    angekommen = set(renames.values())
    for key in ohne:
        if key.item_id in angekommen or key.legacy_id in renames:
            continue
        bisher = by_id.get(key.legacy_id)
        if bisher is not None and not bisher.get("consultation") and (bisher.get("public") is not False) == key.public:
            renames[key.legacy_id] = key.item_id

    taken = existing | {key.item_id for key in keys} | set(renames.values())
    for row in sorted(rows, key=_stored_order):
        legacy_id = str(row["external_id"])
        if legacy_id in renames:
            continue
        base = f"{legacy_id.partition(_AGENDA_ITEM_FRAGMENT)[0]}{_AGENDA_ITEM_FRAGMENT}{_own_key(row)}"
        target, count = base, 1
        while target in taken:
            count += 1
            target = f"{base}-{count}"
        renames[legacy_id] = target
        taken.add(target)
    return renames


@dataclass
class AgendaItemRevert:
    """Rückweg der Kennungen einer Sitzung zur Form von vor Issue #863 (``plan_agenda_item_revert``)."""

    #: umgeschriebene Zeilen: neue Kennung -> genau die abgelöste (``id`` passt wieder zur Kennung)
    restore: dict[str, str] = field(default_factory=dict)
    #: seit der Umstellung neu angelegte Punkte der Tagesordnung: neue Kennung -> die, die der Abruf vor #863 vergäbe
    translate: dict[str, str] = field(default_factory=dict)
    #: umgeschriebene Zeilen ohne Rückweg (abgelöste Kennung unbekannt oder vergeben): Ein Abruf vor #863 könnte an
    #: ihrer ``id`` scheitern
    unresolved: list[str] = field(default_factory=list)

    @property
    def renames(self) -> dict[str, str]:
        return {**self.translate, **self.restore}


def plan_agenda_item_revert(
    agenda: Sequence[Any], stored: Iterable[Mapping[str, Any]], canonical: Callable[[str], Any]
) -> AgendaItemRevert:
    """
    Rückweg einer Sitzung vor einem Rückfall auf einen Abruf von vor Issue #863.

    ``agenda``: die Tagesordnung in ``raw_json`` der Sitzung (Punkte in der Reihenfolge der Seite). ``stored``: ihre
    Punkte im Bestand (``id``, ``external_id``, ``previous_id``). ``canonical``: Kennung (``id``) zu einer Adresse.

    1. Umgeschriebene Zeilen bekommen genau ihre abgelöste Kennung zurück (``previous_id``), wenn ``id`` dazu passt
       und keine andere Zeile sie trägt. Der bisherige Abruf findet sie dann unter ihrer Kennung, statt mit ihrer
       ``id`` eine neue Zeile anlegen zu wollen (Verletzung des Primärschlüssels, Abbruch des ganzen Abrufs).
    2. Seit der Umstellung neu angelegte Punkte, die auf der Tagesordnung stehen, bekommen die Kennung, die der
       bisherige Abruf für sie vergäbe (``legacy_agenda_item_keys``), sofern frei: Er schreibt sie dann fort, statt
       eine zweite Zeile anzulegen. Ihre ``id`` vergibt der bisherige Abruf nie.
    """
    rows = [row for row in stored if row.get("external_id")]
    plan = AgendaItemRevert()
    for row in rows:
        external_id, previous = str(row["external_id"]), row.get("previous_id")
        if is_legacy_agenda_item_id(external_id):
            continue
        if previous and previous != external_id and canonical(str(previous)) == row.get("id"):
            plan.restore[external_id] = str(previous)
        elif canonical(external_id) != row.get("id"):
            plan.unresolved.append(external_id)
    restored = set(plan.restore.values())
    duplicates = {target for target in restored if list(plan.restore.values()).count(target) > 1}
    existing = {str(row["external_id"]) for row in rows}
    for external_id in [e for e, target in plan.restore.items() if target in duplicates or target in existing]:
        plan.unresolved.append(external_id)
        del plan.restore[external_id]

    occupied = (existing - set(plan.restore)) | set(plan.restore.values())
    by_id = {str(row["external_id"]): row for row in rows}
    entries = [entry for entry in agenda if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
    legacy = legacy_agenda_item_keys(
        [
            AgendaRow(number=entry.get("number") or None, title="", public=entry.get("public") is not False)
            for entry in entries
        ]
    )
    for entry, key in zip(entries, legacy, strict=True):
        external_id = entry["id"]
        zeile = by_id.get(external_id)
        if (
            zeile is None
            or external_id in plan.restore
            or zeile.get("previous_id")
            or is_legacy_agenda_item_id(external_id)
        ):
            continue
        target = f"{external_id.partition(_AGENDA_ITEM_FRAGMENT)[0]}{_AGENDA_ITEM_FRAGMENT}{key}"
        if target in occupied:
            continue
        plan.translate[external_id] = target
        occupied.add(target)
        occupied.discard(external_id)
    return plan


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class BodyPlan:
    """Eine Körperschaft, die ein Lauf crawlt (bei einer Körperschaft je Instanz genau eine)."""

    body_id: str
    name: str
    short_name: str
    website: str
    cpanr: int | None = None  # None = Instanz ohne Mandantenparameter
    menu_name: str | None = None  # Name in der Mandantenauswahl (Gegenprobe Kalender)
    terms: list[dict[str, Any]] = field(default_factory=list)

    def params(self) -> dict[str, Any]:
        """Mandantenparameter für Listen-URLs und körperschaftsbezogene Kennungen."""
        return {} if self.cpanr is None else {"__cpanr": self.cpanr}


class SessionNetAdapter:
    """
    Crawlt eine SessionNet-Instanz und liefert synthetische OParl-Dicts.

    Reihenfolge (FK-kompatibel) je Körperschaft: organization -> person ->
    membership, danach je Kalendermonat paper -> meeting -> consultation.

    Mehrere Körperschaften je Instanz (``scraper.bodies``): je Mandant
    (``__cpanr``) ein Body mit eigenen Gremien, Personen und Sitzungen; Listen
    (gr0040, si0040) tragen den Mandantenparameter, Detailseiten nicht (ihre
    Nummern gelten instanzweit). Ohne ``bodies`` bleibt alles wie bisher: ein
    Body je Basis-URL.
    """

    vendor = "sessionnet"
    #: 2: Tagesordnungspunkte heißen nach ihrer Vorlage statt nach ihrer Nummer (Issue #863)
    schema_version = 2

    def __init__(self, config: ScraperConfig, fetcher: PoliteFetcher) -> None:
        self.config = config
        self.fetcher = fetcher
        ext = config.variant or ("php" if ".php" in config.extra.get("hint", "") else None)
        self.urls = SessionNetUrls(base_url=config.base_url, ext=ext or "asp")
        self.stats = ScrapeStats()
        self._crawl_time = datetime.now(TZ_BERLIN).isoformat(timespec="seconds")
        # Monat -> Hash der Kalender-Stubs (Listen-Diffing), wird vom Runner
        # mit dem persistierten Snapshot verglichen und danach gespeichert.
        self.list_snapshots: dict[str, str] = {}
        # Snapshots des letzten Laufs (vom Runner gesetzt): unveränderte
        # Kalendermonate werden im inkrementellen Lauf übersprungen.
        self.previous_snapshots: dict[str, str] = {}
        # Körperschaften dieses Laufs (resolve_bodies) und bereits geladene
        # Gremienlisten je Mandant (kein zweiter Abruf im Crawl)
        self.plans: list[BodyPlan] | None = None
        self._gremien_html: dict[int | None, str | None] = {}
        # Sitzung -> Kennungen ihrer Tagesordnungspunkte, bis der Runner die Sitzung speichert; höchstens für die
        # Sitzungen eines Kalendermonats (Issue #863)
        self._agenda_keys: dict[str, list[AgendaItemKey]] = {}
        #: War die zuletzt mit ``_fetch`` geholte Seite die Fehlerseite "nicht öffentlich" (Fehlercode 1104)?
        self.last_not_public = False

    # -------------------- Hilfen --------------------

    async def _fetch(
        self, url: str, is_detail: bool = False, expected: Callable[[str], bool] | None = None
    ) -> str | None:
        """
        Seite holen; ``None``, wenn sie nicht lesbar ist (Fehler oder fremde Seite, Issue #556).

        Eine Seite ohne SessionNet-Layout (Sperr-, Prüf- oder Hinweisseite mit Status 200) wird nicht
        ausgewertet: Geparst ergäbe sie eine leere Liste, und der Läufer schlösse daraus auf Löschungen.
        ``expected`` prüft zusätzlich den Aufbau der erwarteten Seite (etwa den Kalender).
        """
        if is_detail:
            self.stats.detail_pages_attempted += 1
        self.last_not_public = False
        html = await self.fetcher.fetch_text(url)
        error_code = sessionnet_error_code(html) if html is not None else None
        if html is not None and not is_sessionnet_page(html):
            art = detect_gate(200, html) or "fremder Aufbau"
            console.print(f"[yellow]Scraper: keine SessionNet-Seite ({art}), nicht ausgewertet: {url}[/yellow]")
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "fremde_seite")
            html = None
        elif error_code is not None:
            # Fehlerseite im SessionNet-Layout mit Status 200 (Issue #1075)
            console.print(f"[yellow]Scraper: SessionNet-Fehlerseite ({error_code or 'ohne Code'}): {url}[/yellow]")
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "fehlerseite")
            self.last_not_public = error_code == ERROR_CODE_NOT_PUBLIC
            html = None
        elif html is not None and expected is not None and not expected(html):
            console.print(f"[yellow]Scraper: Seite ohne erwarteten Aufbau, nicht ausgewertet: {url}[/yellow]")
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "fremde_seite")
            html = None
        if html is None:
            self.stats.failed_pages += 1
            # Ein toter Verweis ist keine Störung: Die Quelle sagt selbst, dass es die Seite nicht gibt. Ebenso ein
            # nicht öffentlicher Vorgang (Fehlercode 1104): Die Quelle gibt ihn bewusst nicht heraus.
            if self.last_not_public or is_gone(getattr(self.fetcher, "last_status", None)):
                self.stats.gone_pages += 1
        else:
            self.stats.pages_fetched += 1
        return html

    def _budget_left(self) -> bool:
        budget = self.config.max_detail_pages
        return budget is None or self.stats.detail_pages_attempted < budget

    async def detect_variant(self) -> None:
        """Auto-Detect asp vs. php über die Kalenderseite."""
        if self.config.variant in ("asp", "php"):
            self.urls.ext = self.config.variant
            return
        for ext in ("asp", "php"):
            self.urls.ext = ext
            html = await self.fetcher.fetch_text(self.urls.page("si0040"))
            if html and is_sessionnet_page(html):
                console.print(f"[dim]SessionNet-Variante erkannt: .{ext}[/dim]")
                return
        self.urls.ext = "asp"

    def _file_dict(self, raw: dict[str, str]) -> dict[str, Any]:
        href = raw["href"]
        absolute = urljoin(self.urls.base_url, href)
        file_id = _query_int(href, "id")
        external_id = (
            self.urls.external_id("getfile", id=file_id, type="do")
            if file_id is not None
            else normalize_external_id(absolute, keep_params=("id", "type"))
        )
        mime = "application/pdf" if "pdf" in raw.get("title", "").lower() else None
        return {
            "id": external_id,
            "type": f"{OPARL}File",
            "name": raw.get("name") or raw.get("title") or "Dokument",
            "accessUrl": absolute,
            "downloadUrl": absolute,
            "mimeType": mime,
        }

    # -------------------- Körperschaften --------------------

    def _single_plan(self) -> BodyPlan:
        return BodyPlan(
            body_id=self.urls.body_id(),
            name=self.config.body_name,
            short_name=self.config.body_name,
            website=self.config.base_url,
        )

    def _body_dict(self, plan: BodyPlan) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": plan.body_id,
            "type": f"{OPARL}Body",
            "name": plan.name,
            "shortName": plan.short_name,
            "website": plan.website,
        }
        if plan.terms:
            body["legislativeTerm"] = [
                {
                    "id": self.urls.external_id("gr0040", **plan.params(), __cwpnr=term["wpnr"]),
                    "type": f"{OPARL}LegislativeTerm",
                    "body": plan.body_id,
                    "name": term["name"],
                    "startDate": term["start_date"],
                    "endDate": term["end_date"],
                }
                for term in plan.terms
            ]
        return body

    def build_body(self) -> dict[str, Any]:
        """Body-Dict der ersten Körperschaft (vor resolve_bodies: ohne Wahlperioden)."""
        plan = self.plans[0] if self.plans else self._single_plan()
        return self._body_dict(plan)

    async def resolve_bodies(self) -> list[dict[str, Any]]:
        """
        Ermittelt die Körperschaften des Laufs und liefert ihre Body-Dicts.

        Lädt dafür je Körperschaft die Gremienliste (gr0040), die der Crawl
        ohnehin braucht: Sie nennt die Wahlperioden (``__cwpnr``) und bei
        mehreren Körperschaften den Namen des gewählten Mandanten.
        """
        if self.plans is not None:
            return [self._body_dict(plan) for plan in self.plans]
        await self.detect_variant()
        if self.config.bodies is None:
            plan = self._single_plan()
            html = await self._fetch(self.urls.page("gr0040"))
            self._gremien_html[None] = html
            if html:
                plan.terms = parse_legislative_terms(html)
                others = [m for m in parse_mandanten(html) if m["cpanr"] is not None]
                if others:
                    console.print(
                        f"[yellow]SessionNet-Instanz {self.urls.base_url} führt {len(others) + 1} "
                        "Körperschaften; ohne scraper.bodies wird nur die vorausgewählte gelesen[/yellow]"
                    )
            self.plans = [plan]
            return [self._body_dict(plan)]

        specs = await self._body_specs()
        plans: list[BodyPlan] = []
        for spec in specs:
            html = self._gremien_html.get(spec.cpanr)
            if spec.cpanr not in self._gremien_html:
                html = await self._fetch(self.urls.page("gr0040", __cpanr=spec.cpanr))
                self._gremien_html[spec.cpanr] = html
            selected = next((m for m in parse_mandanten(html) if m["selected"]), None) if html else None
            menu_name = selected["name"] if selected else None
            name = spec.name or menu_name or f"{self.config.body_name} (Mandant {spec.cpanr})"
            plans.append(
                BodyPlan(
                    body_id=self.urls.body_id(spec.cpanr),
                    name=name,
                    short_name=spec.short_name or name,
                    website=self.urls.page("gr0040", __cpanr=spec.cpanr),
                    cpanr=spec.cpanr,
                    menu_name=menu_name,
                    terms=parse_legislative_terms(html) if html else [],
                )
            )
        if not plans:
            console.print(
                f"[yellow]SessionNet-Instanz {self.urls.base_url}: keine Mandantenauswahl gefunden — "
                "eine Körperschaft je Basis-URL[/yellow]"
            )
            self.config.bodies = None
            return await self.resolve_bodies()
        self.plans = plans
        return [self._body_dict(plan) for plan in plans]

    async def _body_specs(self) -> list[BodySpec]:
        """Feste Liste aus der Konfiguration oder ("auto") alle Mandanten der Instanz."""
        if isinstance(self.config.bodies, list):
            return list(self.config.bodies)
        html = await self._fetch(self.urls.page("gr0040"))
        entries = parse_mandanten(html) if html else []
        found: dict[int, str] = {m["cpanr"]: m["name"] for m in entries if m["cpanr"] is not None}
        selected = next((m for m in entries if m["selected"]), None)
        if found and selected is not None:
            # Die Seite nennt die Nummer des vorausgewählten Mandanten nicht;
            # die Gremienliste eines anderen Mandanten führt ihn als Link.
            first = min(found)
            other_html = await self._fetch(self.urls.page("gr0040", __cpanr=first))
            self._gremien_html[first] = other_html
            for entry in parse_mandanten(other_html) if other_html else []:
                if entry["cpanr"] is not None and entry["name"] == selected["name"]:
                    found.setdefault(entry["cpanr"], entry["name"])
            if selected["name"] not in found.values():
                console.print(
                    f"[yellow]SessionNet-Instanz {self.urls.base_url}: Nummer des Mandanten "
                    f"{selected['name']!r} nicht ermittelbar — scraper.bodies als Liste angeben[/yellow]"
                )
        return [BodySpec(cpanr=cpanr) for cpanr in sorted(found)]

    # -------------------- Crawl --------------------

    async def iter_entities(self, window: CrawlWindow, full: bool) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        """Wie iter_body_entities, ohne Body-Kennung (eine Körperschaft je Instanz)."""
        async for _body_id, entity_type, page in self.iter_body_entities(window, full):
            yield entity_type, page

    async def iter_body_entities(
        self, window: CrawlWindow, full: bool
    ) -> AsyncIterator[tuple[str, str, list[dict[str, Any]]]]:
        """Yield (Body-Kennung, entity_type, Seite) je Körperschaft in FK-Reihenfolge."""
        if self.plans is None:
            await self.resolve_bodies()
        assert self.plans is not None
        # Vorlagen je Lauf nur einmal laden, auch über Körperschaften hinweg
        fetched_papers: set[int] = set()
        for plan in self.plans:
            async for entity_type, page in self._crawl_body(plan, window, full, fetched_papers):
                yield plan.body_id, entity_type, page

    @staticmethod
    def is_removable_agenda_item_id(external_id: str) -> bool:
        """
        Darf der Löschabgleich der Tagesordnung diesen Punkt markieren? Nur Punkte in der Form seit Issue #863;
        Zeilen in der bisherigen Form schreibt erst die Umstellung um (``agenda_item_renames``).
        """
        _, fragment, key = external_id.partition(_AGENDA_ITEM_FRAGMENT)
        return bool(fragment) and key.startswith(AGENDA_ITEM_KEY_PREFIXES)

    def agenda_item_renames(self, meeting_id: str, stored: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        """
        Bisherige Kennungen der Tagesordnungspunkte einer Sitzung im Bestand -> neue (Issue #863).

        Der Runner fragt vor dem Speichern jeder Sitzung, ``stored`` sind deren Punkte im Bestand
        (``plan_agenda_item_renames``). Danach vergisst der Adapter die Kennungen der Sitzung.
        """
        keys = self._agenda_keys.pop(meeting_id, None)
        if not keys:
            return {}
        return plan_agenda_item_renames(keys, stored)

    async def _crawl_body(
        self,
        plan: BodyPlan,
        window: CrawlWindow,
        full: bool,
        fetched_papers: set[int],
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        body_id = plan.body_id

        # ---------------- Gremien ----------------
        org_by_name: dict[str, str] = {}
        org_dicts: list[dict[str, Any]] = []
        # Gremien ohne veröffentlichte Mitglieder (kein Link auf kp0040, Issue #1075): keine Mitgliederliste abrufen
        without_members: set[str] = set()
        if plan.cpanr in self._gremien_html:
            gr_html = self._gremien_html[plan.cpanr]
        else:
            gr_html = await self._fetch(self.urls.page("gr0040", **plan.params()))
        if not gr_html:
            self.stats.mark_incomplete("Gremienliste nicht lesbar", "organization", "person", "membership")
        if gr_html:
            orgs = parse_organizations(gr_html)
            if not orgs:
                self.stats.parse_failures += 1
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "gr0040")
                self.stats.mark_incomplete("Gremienliste ohne Gremien", "organization", "person", "membership")
            for org in orgs:
                external_id = self.urls.external_id("kp0040", __kgrnr=org["kgrnr"])
                # Sitzungen finden ihr Gremium über den Namen; bei gleichnamigen Gremien zählt eines mit Mitgliederseite
                if org["members_page"] or org["name"] not in org_by_name:
                    org_by_name[org["name"]] = external_id
                org_dict: dict[str, Any] = {
                    "id": external_id,
                    "type": f"{OPARL}Organization",
                    "body": body_id,
                    "name": org["name"],
                    "organizationType": "Gremium",
                    "modified": self._crawl_time,
                }
                if org["members_page"]:
                    org_dict["web"] = self.urls.page("kp0040", __kgrnr=org["kgrnr"])
                else:
                    without_members.add(external_id)
                org_dicts.append(with_content_hash(org_dict))
            self.stats.entities_parsed += len(org_dicts)
        if org_dicts:
            yield ("organization", org_dicts)

        # ---------------- Personen + Mitgliedschaften ----------------
        if org_dicts and (full or not self.config.members_on_full_only):
            persons: dict[str, dict[str, Any]] = {}
            memberships: list[dict[str, Any]] = []
            for org in org_dicts:
                if org["id"] in without_members:
                    continue
                if not self._budget_left():
                    self.stats.mark_incomplete("Detailseiten-Budget erreicht", "person", "membership")
                    break
                kgrnr = _query_int(org["id"], "__kgrnr")
                if kgrnr is None:
                    continue
                html = await self._fetch(self.urls.page("kp0040", __kgrnr=kgrnr), is_detail=True)
                if not html:
                    self.stats.parse_failures += 1
                    metrics.record_scraper_parse_failure(self.fetcher.source_name, "kp0040")
                    self.stats.mark_incomplete("Mitgliederliste nicht lesbar", "person", "membership")
                    continue
                rows = parse_members(html)
                self.stats.detail_pages_parsed += 1
                for row in rows:
                    if row["kpenr"] is not None:
                        # Bei mehreren Körperschaften je Körperschaft eine Person:
                        # Wer im Samtgemeinderat und im Rat einer Mitgliedsgemeinde
                        # sitzt, erscheint in beiden Bodies vollständig.
                        person_id = self.urls.external_id("pe0051", **plan.params(), __kpenr=row["kpenr"])
                        member_key: Any = row["kpenr"]
                    else:
                        # Ohne Detailseite: stabiles Fragment-Schema auf
                        # Basis der Body-URL + normalisiertem Namen
                        member_key = _slug(row["name"])
                        person_id = f"{body_id}#person/{member_key}"
                    persons.setdefault(
                        person_id,
                        with_content_hash(
                            {
                                "id": person_id,
                                "type": f"{OPARL}Person",
                                "body": body_id,
                                "name": row["name"],
                                "modified": self._crawl_time,
                            }
                        ),
                    )
                    membership: dict[str, Any] = {
                        "id": f"{org['id']}#membership/{member_key}",
                        "type": f"{OPARL}Membership",
                        "person": person_id,
                        "organization": org["id"],
                        "role": row["role"],
                        "votingRight": row["voting_right"],
                        "modified": self._crawl_time,
                    }
                    # Beginn und Ende nur, wenn die Seite sie nennt (beendete Mitgliedschaften, Issue #1075)
                    if row.get("start_date"):
                        membership["startDate"] = row["start_date"]
                    if row.get("end_date"):
                        membership["endDate"] = row["end_date"]
                    memberships.append(with_content_hash(membership))
                self.stats.entities_parsed += len(rows)
            if persons:
                yield ("person", list(persons.values()))
            if memberships:
                yield ("membership", memberships)

        # ---------------- Sitzungskalender (Fenster) ----------------
        budget = self.config.max_detail_pages
        for year, month in window.months():
            if not self._budget_left():
                console.print(
                    f"[yellow]Scraper: Detailseiten-Budget erreicht "
                    f"({budget}) — Crawl endet vor {month:02d}/{year}[/yellow]"
                )
                self.stats.mark_incomplete("Detailseiten-Budget erreicht", "meeting", "paper", "consultation")
                break
            cal_html = await self._fetch(
                self.urls.page("si0040", **plan.params(), __cjahr=year, __cmonat=month, __canz=1),
                expected=is_calendar_page,
            )
            if not cal_html:
                self.stats.parse_failures += 1
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0040")
                self.stats.mark_incomplete(
                    f"Kalender {month:02d}/{year} nicht lesbar", "meeting", "paper", "consultation"
                )
                continue
            stubs = sorted(parse_calendar(cal_html), key=MeetingStub.sort_key)
            if plan.cpanr is not None and plan.menu_name:
                # Gegenprobe: Einträge eines anderen Mandanten (Instanz hat
                # __cpanr nicht beachtet) gehören nicht zu dieser Körperschaft.
                row_mandants = parse_calendar_mandanten(cal_html)
                foreign = [s for s in stubs if row_mandants.get(s.ksinr, plan.menu_name) != plan.menu_name]
                if foreign:
                    console.print(
                        f"[yellow]Scraper: {len(foreign)} Sitzung(en) im Kalender {month:02d}/{year} gehören "
                        f"nicht zu Mandant {plan.cpanr} — übersprungen[/yellow]"
                    )
                    stubs = [s for s in stubs if s not in foreign]
            month_key = f"{year:04d}-{month:02d}"
            if plan.cpanr is not None:
                month_key = f"p{plan.cpanr}:{month_key}"
            snapshot = with_content_hash({"stubs": [_snapshot_entry(s) for s in stubs]})["mandari:contentHash"]
            self.list_snapshots[month_key] = snapshot
            if not full and self.previous_snapshots.get(month_key) == snapshot:
                # Listen-Diffing: Kalendermonat unverändert -> Detailseiten
                # dieses Monats überspringen (spart die teuren Requests).
                continue

            papers: list[dict[str, Any]] = []
            meetings: list[dict[str, Any]] = []
            consultations: list[dict[str, Any]] = []
            # Die Sitzungen des Vormonats hat der Runner gespeichert (oder als unverändert übersprungen)
            self._agenda_keys.clear()

            for stub in stubs:
                if not self._budget_left():
                    self.stats.mark_incomplete("Detailseiten-Budget erreicht", "meeting", "paper", "consultation")
                    break
                meeting = await self._build_meeting(stub, body_id, org_by_name)
                if meeting is None:
                    # Der Kalender nennt die Sitzung: Es gibt sie, nur ihre Seite war nicht lesbar
                    self.stats.mark_listed("meeting", self.urls.external_id("si0057", __ksinr=stub.ksinr))
                    self.stats.mark_incomplete(f"Sitzung {stub.ksinr} nicht lesbar", "paper", "consultation")
                    continue
                meetings.append(meeting)
                # Vorlagen der TOPs nachladen (dedupliziert je Lauf)
                for consultation in meeting.pop("mandari:consultations", []):
                    kvonr = consultation.pop("mandari:kvonr")
                    consultations.append(with_content_hash(consultation))
                    if kvonr in fetched_papers:
                        continue
                    if not self._budget_left():
                        self.stats.mark_incomplete("Detailseiten-Budget erreicht", "paper")
                        continue
                    fetched_papers.add(kvonr)
                    paper = await self._build_paper(kvonr, body_id)
                    if paper is not None:
                        papers.append(paper)
                    else:
                        self.stats.mark_listed("paper", self.urls.external_id("vo0050", __kvonr=kvonr))

            self.stats.entities_parsed += len(papers) + len(meetings) + len(consultations)
            if papers:
                yield ("paper", papers)
            if meetings:
                yield ("meeting", meetings)
            if consultations:
                yield ("consultation", consultations)

    async def _build_meeting(
        self,
        stub: MeetingStub,
        body_id: str,
        org_by_name: dict[str, str],
    ) -> dict[str, Any] | None:
        external_id = self.urls.external_id("si0057", __ksinr=stub.ksinr)
        agenda = MeetingAgenda()
        info: dict[str, Any] = {}
        # Angekündigte Sitzung ohne veröffentlichte Tagesordnung oder nicht öffentliche Sitzungsseite (Fehlercode
        # 1104): nur die Angaben aus dem Kalender, unter derselben Kennung wie später mit Tagesordnung (Issue #1075)
        if stub.agenda_linked:
            agenda_html = await self._fetch(self.urls.page("si0057", __ksinr=stub.ksinr), is_detail=True)
            if agenda_html:
                agenda = parse_meeting_agenda(agenda_html)
                info_html = await self._fetch(self.urls.page("si0050", __ksinr=stub.ksinr))
                if info_html:
                    info = parse_meeting_info(info_html)
            elif not self.last_not_public:
                self.stats.parse_failures += 1
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0057")
                return None

        name = stub.name or agenda.title
        if not name:
            self.stats.parse_failures += 1
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0057")
            return None
        if stub.agenda_linked:
            self.stats.detail_pages_parsed += 1

        meeting: dict[str, Any] = {
            "id": external_id,
            "type": f"{OPARL}Meeting",
            "body": body_id,
            "name": name,
            "modified": self._crawl_time,
        }

        date_str = info.get("date") or stub.date
        time_str = info.get("time") or ""
        time_match = _TIME_RANGE_RE.match(time_str.replace("\xa0", " ").strip())
        start_time = (time_match.group(1) if time_match else None) or stub.time_start
        end_time = (time_match.group(2) if time_match else None) or stub.time_end
        if date_str:
            start = _parse_german_datetime(date_str, start_time)
            if start:
                meeting["start"] = start
            if end_time:
                end = _parse_german_datetime(date_str, end_time)
                if end:
                    meeting["end"] = end

        location = info.get("room") or stub.location
        if location:
            meeting["location"] = {
                "id": f"{body_id}#location/{_slug(location)}",
                "type": f"{OPARL}Location",
                "description": location,
            }

        committee = info.get("committee") or stub.committee
        if committee:
            org_ref = org_by_name.get(committee)
            meeting["organization"] = [org_ref] if org_ref else []
            meeting["mandari:committee"] = committee

        # Sitzungsdokumente (Einladung/Tagesordnung/Niederschrift)
        auxiliary: list[dict[str, Any]] = []
        for raw in info.get("files", []):
            file_dict = self._file_dict(raw)
            lowered = (file_dict.get("name") or "").lower()
            if "einladung" in lowered and "invitation" not in meeting:
                meeting["invitation"] = file_dict
            elif ("niederschrift" in lowered or "protokoll" in lowered) and ("resultsProtocol" not in meeting):
                meeting["resultsProtocol"] = file_dict
            else:
                auxiliary.append(file_dict)
        if auxiliary:
            meeting["auxiliaryFile"] = auxiliary

        # Tagesordnung + Beratungen. Kennung je Punkt aus der Vorlage, sonst aus der Nummer (Issue #863); der
        # nichtöffentliche Teil zählt oft neu ("Ö 1" … "N 1") und hat deshalb eigene Nummern-Kennungen.
        agenda_items: list[dict[str, Any]] = []
        consultations: list[dict[str, Any]] = []
        keys: list[AgendaItemKey] = []
        rows = zip(agenda.rows, agenda_item_keys(agenda.rows), legacy_agenda_item_keys(agenda.rows), strict=True)
        for index, (row, key, legacy_key) in enumerate(rows, start=1):
            item_id = f"{external_id}{_AGENDA_ITEM_FRAGMENT}{key}"
            item: dict[str, Any] = {
                "id": item_id,
                "type": f"{OPARL}AgendaItem",
                "meeting": external_id,
                "number": row.number,
                "order": index,
                "name": row.title,
                "public": row.public,
            }
            if row.result:
                item["result"] = row.result
            if row.vote:
                item["mandari:vote"] = row.vote
            if row.paper_kvonr is not None:
                paper_id = self.urls.external_id("vo0050", __kvonr=row.paper_kvonr)
                consultation_id = f"{paper_id}#consultation/{stub.ksinr}"
                item["consultation"] = consultation_id
                consultations.append(
                    {
                        "id": consultation_id,
                        "type": f"{OPARL}Consultation",
                        "paper": paper_id,
                        "meeting": external_id,
                        "agendaItem": item_id,
                        "modified": self._crawl_time,
                        "mandari:kvonr": row.paper_kvonr,
                    }
                )
            agenda_items.append(item)
            keys.append(
                AgendaItemKey(
                    item_id=item_id,
                    legacy_id=f"{external_id}{_AGENDA_ITEM_FRAGMENT}{legacy_key}",
                    name=row.title,
                    public=row.public,
                    consultation=item.get("consultation"),
                )
            )
        if agenda_items:
            meeting["agendaItem"] = agenda_items
            # Gelesene Tagesordnung (mindestens ein Punkt): Grundlage des Löschabgleichs ihrer Punkte im Runner. Fehlt
            # ein Teil der Seite, hält die Bremse je Sitzung den Abgleich an (``_missing_agenda_items``).
            self.stats.agendas[external_id] = {item["id"] for item in agenda_items}
        self._agenda_keys[external_id] = keys
        # Interner Transport zum Aufrufer (wird vor dem Upsert entfernt)
        meeting["mandari:consultations"] = consultations

        result = with_content_hash({k: v for k, v in meeting.items() if k != "mandari:consultations"})
        result["mandari:consultations"] = consultations
        return result

    async def _build_paper(self, kvonr: int, body_id: str) -> dict[str, Any] | None:
        external_id = self.urls.external_id("vo0050", __kvonr=kvonr)
        html = await self._fetch(self.urls.page("vo0050", __kvonr=kvonr), is_detail=True)
        if not html:
            self.stats.parse_failures += 1
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "vo0050")
            return None
        parsed = parse_paper(html)
        if not parsed.get("name"):
            self.stats.parse_failures += 1
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "vo0050")
            return None
        self.stats.detail_pages_parsed += 1

        paper: dict[str, Any] = {
            "id": external_id,
            "type": f"{OPARL}Paper",
            "body": body_id,
            "name": parsed["name"],
            "modified": self._crawl_time,
        }
        if parsed.get("reference"):
            paper["reference"] = parsed["reference"]
        if parsed.get("paper_type"):
            paper["paperType"] = parsed["paper_type"]

        files = [self._file_dict(raw) for raw in parsed.get("files", [])]
        if files:
            main = next(
                (f for f in files if (f.get("name") or "").strip().lower() == "vorlage"),
                files[0],
            )
            paper["mainFile"] = main
            rest = [f for f in files if f is not main]
            if rest:
                paper["auxiliaryFile"] = rest
        return with_content_hash(paper)


def _parse_german_datetime(date_str: str, time_str: str | None) -> str | None:
    """dd.mm.yyyy + HH:MM -> ISO 8601 mit Europe/Berlin-Offset."""
    try:
        dt = datetime.strptime(date_str.strip(), "%d.%m.%Y")
        if time_str:
            hour, minute = time_str.strip().split(":")
            dt = dt.replace(hour=int(hour), minute=int(minute))
        return dt.replace(tzinfo=TZ_BERLIN).isoformat(timespec="seconds")
    except (ValueError, AttributeError):
        return None


def _snapshot_entry(stub: MeetingStub) -> dict[str, Any]:
    """Kalendereintrag im Listen-Snapshot; ``agenda_linked`` nur bei angekündigten Sitzungen (Hash sonst wie bisher)."""
    entry = vars(stub).copy()
    if stub.agenda_linked:
        entry.pop("agenda_linked")
    return entry


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "unbekannt"
