"""
SessionNet-Adapter (Somacos Session/SessionNet, Bürgerinfo-Frontend).

Verifiziert gegen SessionNet 5.5 (Layout 6) an zwei realen Instanzen:
- buergerinfo.luedenscheid.de (klassisch, *.asp)
- rat.eschweiler.de/bi (PHP-Variante, *.php — identisches Markup)

Seitenkürzel (produktweit stabil):
- si0040   Sitzungskalender (Monatsansicht, __cjahr/__cmonat/__canz)
- si0050   Sitzungsdetail "Informationen" (Gremium, Datum, Zeit, Raum, Dokumente)
- si0057   Sitzungsdetail "Tagesordnung" (TOPs Ö/NÖ, Beschlüsse, Vorlagen-Links)
- vo0050   Vorlagendetail (__kvonr; Betreff, Nummer, Art, Anlagen-PDFs)
- gr0040   Gremienliste
- kp0040   Gremium-Mitglieder (__kgrnr; Personen mit pe0051-Links)
- getfile  Datei-Download (id, type=do)

external_id = normalisierte kanonische Detail-URL (nur ID-Parameter, sortiert).
Ausgabe: synthetische OParl-1.1-Dicts für die bestehende Pipeline.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from rich.console import Console

from src.metrics import metrics
from src.scrapers.base import (
    CrawlWindow,
    ScraperConfig,
    ScrapeStats,
    normalize_external_id,
    with_content_hash,
)
from src.scrapers.politeness import PoliteFetcher

console = Console()

OPARL = "https://schema.oparl.org/1.1/"
TZ_BERLIN = ZoneInfo("Europe/Berlin")

_TIME_RANGE_RE = re.compile(r"^(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?(?:\s*Uhr)?$")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")


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
            keep_params=("__ksinr", "__kvonr", "__kgrnr", "__kpenr", "id", "type"),
        )

    def body_id(self) -> str:
        return normalize_external_id(self.base_url, keep_params=())


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

    def sort_key(self) -> tuple:
        return (self.date or "", self.ksinr)


def parse_calendar(html: str) -> list[MeetingStub]:
    """Parst die Monatsansicht des Sitzungskalenders (si0040)."""
    soup = BeautifulSoup(html, "html.parser")
    stubs: list[MeetingStub] = []
    for cell in soup.select("td.silink"):
        link = cell.find("a", href=re.compile(r"si0057\.(asp|php)\?"))
        if link is None:
            continue
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
        raw_number = _clean(badge.get_text() if badge else num_cell.get_text())
        # "Ö 3.2" / "N 1" -> "3.2" / "1"; Ö/N ist redundant zur Sektion
        number = re.sub(r"^[ÖöNn]\s+", "", raw_number.replace("\xa0", " ")).strip() or None

        title_div = title_cell.find("div", class_=re.compile(r"smc-card-header-title"))
        title = _clean(title_div.get_text() if title_div else title_cell.get_text())
        if not title:
            continue

        item = AgendaRow(number=number, title=title, public=public)

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
    """Parst die Gremienliste (gr0040): kgrnr + Name."""
    soup = BeautifulSoup(html, "html.parser")
    orgs: dict[int, dict[str, Any]] = {}
    for cell in soup.select("td.grname"):
        link = cell.find("a", href=re.compile(r"kp0040\.(asp|php)\?"))
        if link is None:
            continue
        kgrnr = _query_int(link.get("href", ""), "__kgrnr")
        name = _clean(link.get_text())
        if kgrnr is None or not name:
            continue
        orgs.setdefault(kgrnr, {"kgrnr": kgrnr, "name": name})
    return list(orgs.values())


def parse_members(html: str) -> list[dict[str, Any]]:
    """
    Parst die Mitgliederliste eines Gremiums (kp0040).

    Liefert Zeilen mit kpenr, Name, Partei/Mitgliedschaft, Funktion und
    Stimmrecht (aus der Abschnittsüberschrift, z. B. "... ohne Stimmrecht:").
    """
    soup = BeautifulSoup(html, "html.parser")
    members: list[dict[str, Any]] = []
    section: str | None = None
    for row in soup.find_all("tr"):
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
        party_cell = row.find("td", class_="pepartei")
        role_cell = row.find("td", class_="amname")
        voting = True
        if section:
            lowered = section.lower()
            # "... ohne Stimmrecht", "mit beratender Stimme", "Beratende
            # Mitglieder ..." => kein Stimmrecht
            if "ohne stimmrecht" in lowered or "beratend" in lowered:
                voting = False
        members.append(
            {
                "kpenr": kpenr,
                "name": name,
                "party": _clean(party_cell.get_text()) if party_cell else None,
                "role": (_clean(role_cell.get_text()) if role_cell else None) or section,
                "section": section,
                "voting_right": voting,
            }
        )
    return members


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SessionNetAdapter:
    """
    Crawlt eine SessionNet-Instanz und liefert synthetische OParl-Dicts.

    Reihenfolge (FK-kompatibel): organization -> person -> membership,
    danach je Kalendermonat paper -> meeting -> consultation.
    """

    vendor = "sessionnet"
    schema_version = 1

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

    # -------------------- Hilfen --------------------

    async def _fetch(self, url: str, is_detail: bool = False) -> str | None:
        if is_detail:
            self.stats.detail_pages_attempted += 1
        html = await self.fetcher.fetch_text(url)
        if html is not None:
            self.stats.pages_fetched += 1
        return html

    async def detect_variant(self) -> None:
        """Auto-Detect asp vs. php über die Kalenderseite."""
        if self.config.variant in ("asp", "php"):
            self.urls.ext = self.config.variant
            return
        for ext in ("asp", "php"):
            self.urls.ext = ext
            html = await self.fetcher.fetch_text(self.urls.page("si0040"))
            if html and "sessionnet" in html.lower():
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

    def build_body(self) -> dict[str, Any]:
        return {
            "id": self.urls.body_id(),
            "type": f"{OPARL}Body",
            "name": self.config.body_name,
            "shortName": self.config.body_name,
            "website": self.config.base_url,
        }

    # -------------------- Crawl --------------------

    async def iter_entities(
        self, window: CrawlWindow, full: bool
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        await self.detect_variant()
        body_id = self.urls.body_id()
        detail_budget = self.config.max_detail_pages

        def budget_left() -> bool:
            return detail_budget is None or self.stats.detail_pages_attempted < detail_budget

        # ---------------- Gremien ----------------
        org_by_name: dict[str, str] = {}
        org_dicts: list[dict[str, Any]] = []
        gr_html = await self._fetch(self.urls.page("gr0040"))
        if gr_html:
            orgs = parse_organizations(gr_html)
            if not orgs:
                self.stats.parse_failures += 1
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "gr0040")
            for org in orgs:
                external_id = self.urls.external_id("kp0040", __kgrnr=org["kgrnr"])
                org_by_name[org["name"]] = external_id
                org_dicts.append(
                    with_content_hash(
                        {
                            "id": external_id,
                            "type": f"{OPARL}Organization",
                            "body": body_id,
                            "name": org["name"],
                            "organizationType": "Gremium",
                            "web": self.urls.page("kp0040", __kgrnr=org["kgrnr"]),
                            "modified": self._crawl_time,
                        }
                    )
                )
            self.stats.entities_parsed += len(org_dicts)
        if org_dicts:
            yield ("organization", org_dicts)

        # ---------------- Personen + Mitgliedschaften ----------------
        if org_dicts and (full or not self.config.members_on_full_only):
            persons: dict[str, dict[str, Any]] = {}
            memberships: list[dict[str, Any]] = []
            for org in org_dicts:
                if not budget_left():
                    break
                kgrnr = _query_int(org["id"], "__kgrnr")
                if kgrnr is None:
                    continue
                html = await self._fetch(self.urls.page("kp0040", __kgrnr=kgrnr), is_detail=True)
                if not html:
                    self.stats.parse_failures += 1
                    metrics.record_scraper_parse_failure(self.fetcher.source_name, "kp0040")
                    continue
                rows = parse_members(html)
                self.stats.detail_pages_parsed += 1
                for row in rows:
                    if row["kpenr"] is not None:
                        person_id = self.urls.external_id("pe0051", __kpenr=row["kpenr"])
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
                    memberships.append(
                        with_content_hash(
                            {
                                "id": f"{org['id']}#membership/{member_key}",
                                "type": f"{OPARL}Membership",
                                "person": person_id,
                                "organization": org["id"],
                                "role": row["role"],
                                "votingRight": row["voting_right"],
                                "modified": self._crawl_time,
                            }
                        )
                    )
                self.stats.entities_parsed += len(rows)
            if persons:
                yield ("person", list(persons.values()))
            if memberships:
                yield ("membership", memberships)

        # ---------------- Sitzungskalender (Fenster) ----------------
        fetched_papers: set[int] = set()
        for year, month in window.months():
            if not budget_left():
                console.print(
                    f"[yellow]Scraper: Detailseiten-Budget erreicht "
                    f"({detail_budget}) — Crawl endet vor {month:02d}/{year}[/yellow]"
                )
                break
            cal_html = await self._fetch(
                self.urls.page("si0040", __cjahr=year, __cmonat=month, __canz=1)
            )
            if not cal_html:
                self.stats.parse_failures += 1
                metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0040")
                continue
            stubs = sorted(parse_calendar(cal_html), key=MeetingStub.sort_key)
            month_key = f"{year:04d}-{month:02d}"
            snapshot = with_content_hash(
                {"stubs": [vars(s) for s in stubs]}
            )["mandari:contentHash"]
            self.list_snapshots[month_key] = snapshot
            if not full and self.previous_snapshots.get(month_key) == snapshot:
                # Listen-Diffing: Kalendermonat unverändert -> Detailseiten
                # dieses Monats überspringen (spart die teuren Requests).
                continue

            papers: list[dict[str, Any]] = []
            meetings: list[dict[str, Any]] = []
            consultations: list[dict[str, Any]] = []

            for stub in stubs:
                if not budget_left():
                    break
                meeting = await self._build_meeting(stub, body_id, org_by_name)
                if meeting is None:
                    continue
                meetings.append(meeting)
                # Vorlagen der TOPs nachladen (dedupliziert je Lauf)
                for consultation in meeting.pop("mandari:consultations", []):
                    kvonr = consultation.pop("mandari:kvonr")
                    consultations.append(with_content_hash(consultation))
                    if kvonr in fetched_papers or not budget_left():
                        continue
                    fetched_papers.add(kvonr)
                    paper = await self._build_paper(kvonr, body_id)
                    if paper is not None:
                        papers.append(paper)

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
        agenda_html = await self._fetch(
            self.urls.page("si0057", __ksinr=stub.ksinr), is_detail=True
        )
        if not agenda_html:
            self.stats.parse_failures += 1
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0057")
            return None
        agenda = parse_meeting_agenda(agenda_html)

        info: dict[str, Any] = {}
        info_html = await self._fetch(self.urls.page("si0050", __ksinr=stub.ksinr))
        if info_html:
            info = parse_meeting_info(info_html)

        name = stub.name or agenda.title
        if not name:
            self.stats.parse_failures += 1
            metrics.record_scraper_parse_failure(self.fetcher.source_name, "si0057")
            return None
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
                "id": f"{self.urls.body_id()}#location/{_slug(location)}",
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
            elif ("niederschrift" in lowered or "protokoll" in lowered) and (
                "resultsProtocol" not in meeting
            ):
                meeting["resultsProtocol"] = file_dict
            else:
                auxiliary.append(file_dict)
        if auxiliary:
            meeting["auxiliaryFile"] = auxiliary

        # Tagesordnung + Beratungen
        agenda_items: list[dict[str, Any]] = []
        consultations: list[dict[str, Any]] = []
        for index, row in enumerate(agenda.rows, start=1):
            item_id = f"{external_id}#agendaitem/{row.number or index}"
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
        if agenda_items:
            meeting["agendaItem"] = agenda_items
        # Interner Transport zum Aufrufer (wird vor dem Upsert entfernt)
        meeting["mandari:consultations"] = consultations

        result = with_content_hash(
            {k: v for k, v in meeting.items() if k != "mandari:consultations"}
        )
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


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "unbekannt"
