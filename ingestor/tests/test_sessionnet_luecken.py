"""
SessionNet: Lücken an weiteren Instanzen (Issue #1075).

Die Ausschnitte folgen dem Markup zweier öffentlicher SessionNet-Instanzen (5.4.5 und 5.5.0, ``*.php``, Layout 6:
eine Samtgemeinde, ein Landkreis), abgerufen im Oktober 2026 mit unserem User-Agent; Namen, Orte, Personen und
Nummern sind erfunden, die Seiten auf das Nötige gekürzt. Kein Netzwerk.

1. Gremien ohne Mitgliederseite (kein Link auf kp0040) mit der Nummer aus der Sitzungsliste (si0046)
2. Beendete Mitgliedschaften ("Mitgliedschaft beendet:") mit Ende statt als aktuelle Mitglieder
3. Fehlerseite von SessionNet mit Status 200 ("Fehlercode: 1104") nicht als Inhalt auswerten
4. Angekündigte Sitzungen ohne Link auf die Tagesordnung aus dem Kalender (Kennung im Termin-Link)
5. Öffentlich/nichtöffentlich aus dem Kennzeichen des Punkts, wenn eine Abschnittsüberschrift fehlt
6. Eine beendete Mitgliedschaft überschreibt nicht die aktuelle derselben Person; angekündigte Sitzungen in der
   Gegenprobe "Mandant"; nicht öffentliche Vorlagen zählen nicht gegen die Parse-Quote
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.scrapers.base import BodySpec, CrawlWindow, ScraperConfig
from src.scrapers.sessionnet import (
    SessionNetAdapter,
    is_sessionnet_page,
    membership_keys,
    parse_calendar,
    parse_calendar_mandanten,
    parse_meeting_agenda,
    parse_members,
    parse_organizations,
    sessionnet_error_code,
)
from src.sync.processor import OParlProcessor

BASE_URL = "https://ratsinfo.musterheide.example/sessionnet/bi/"
NOVEMBER = CrawlWindow(start=date(2026, 11, 1), end=date(2026, 11, 30))


def page(content: str, title: str = "Gremien") -> str:
    """Seite im SessionNet-Layout (Rahmen ``smclayout``) mit Inhalt."""
    return (
        f'<html><head><meta charset="utf-8"><title>SessionNet | {title}</title></head><body id="smc_body">'
        f'<div id="wrapper" class="smclayout"><div id="page-content" role="main">{content}</div></div></body></html>'
    )


#: Fehlerseite für einen nicht öffentlichen Vorgang (Rahmen ``smc_page_error_layout``, Status 200)
ERROR_PAGE = (
    '<html><head><title>SessionNet | SessionNet Fehlermeldung</title></head><body id="smc_body">'
    '<div id="wrapper" class="smclayout smc_page_error_layout"><div class="page-title">'
    '<h1 class="smc_h1">SessionNet Fehlermeldung</h1></div><div id="page-content" role="main">'
    '<h3 class="smc_h3">Zum &Ouml;ffnen des Vorgangs fehlt die Berechtigung.<br /><br /></h3>'
    "<br /><br />Fehlercode: 1104</div></div></body></html>"
)

GREMIEN = page(
    '<table id="smc_page_gr0040_contenttable1" class="table smc-table smctablegremien"><tbody>'
    # Gremium mit Mitgliederseite
    '<tr class="smc-t-r-l"><td class="smc-t-cl991 grname"><a href="kp0040.php?__kgrnr=1&" '
    'title="Details anzeigen: Rat der Samtgemeinde Musterheide">Rat der Samtgemeinde Musterheide</a></td>'
    '<td class="smc-t-cn991 silast"></td><td class="smc-t-cn991 sinext"></td><td class="smc-t-cl991 smc-link-si2">'
    '<a href="si0046.php?smccont=85&__cselect=65536&__cfid=65536&__canz=12&__cmonat=1&__osidat=d&__ksigrnr=1" '
    'title="Sitzungsliste anzeigen"><i class="smc smc-calendar"></i></a></td></tr>'
    # Mitglieder nicht veröffentlicht: kein Link auf kp0040, Nummer nur in der Sitzungsliste
    '<tr class="smc-t-r-l"><td class="smc-t-cl991 grname">Samtgemeindeausschuss</td>'
    '<td class="smc-t-cn991 silast"></td><td class="smc-t-cn991 sinext"></td><td class="smc-t-cl991 smc-link-si2">'
    '<a href="si0046.php?smccont=85&__cselect=65536&__cfid=65536&__canz=12&__cmonat=1&__osidat=d&__ksigrnr=8" '
    'title="Sitzungsliste anzeigen"><i class="smc smc-calendar"></i></a></td></tr>'
    # Ohne jeden Link: keine stabile Kennung
    '<tr class="smc-t-r-l"><td class="smc-t-cl991 grname">Verwaltungsausschuss</td><td class="silast"></td>'
    '<td class="sinext"></td><td class="smc-t-cn991 smc-link-si2"></td></tr>'
    "</tbody></table>"
)

MITGLIEDER = page(
    '<h2 class="smc_h2">Mandatsträger:</h2>'
    '<table id="smc_page_kp0040_contenttable1" class="table smc-table"><tbody>'
    '<tr class="smc-t-r-l"><td class="smcfield_puname" colspan="3">Mitglieder</td></tr>'
    '<tr class="smc-t-r-l"><td class="pename"><a href="pe0051.php?__kpenr=11">Erika Mustermann</a></td>'
    '<td class="pepartei">Liste A</td><td class="pepartei">Liste A Mitglied</td><td class="amname">Mitglied</td></tr>'
    '<tr class="smc-t-r-l"><td class="smcfield_puname" colspan="3">zur Beratung hinzugezogen</td></tr>'
    '<tr class="smc-t-r-l"><td class="pename"><a href="pe0051.php?__kpenr=12">Max Beispiel</a></td>'
    '<td class="pepartei">Gast</td><td class="amname">Hinzugezogen</td></tr>'
    '<tr class="smc-t-r-l"><td class="smcfield_puname" colspan="3">Verwaltung</td></tr>'
    '<tr class="smc-t-r-l"><td class="pename"><a href="pe0051.php?__kpenr=13">Paula Protokoll</a></td>'
    '<td class="pepartei">Fachbereich Rat</td><td class="amname">Verwaltung</td></tr>'
    "</tbody></table>"
    '<h2 class="smc_h2">Mitgliedschaft beendet:</h2>'
    '<table id="smc_page_kp0040_contenttable2" class="table smc-table"><thead><tr><th class="pename">Name</th>'
    '<th class="pepartei">Mitgliedschaft</th><th class="amname">Art der Mitarbeit</th><th class="mgadat">von</th>'
    '<th class="mgedat">bis</th></tr></thead><tbody>'
    '<tr class="smc-t-r-l"><td class="pename">Hans Frueher</td><td class="pepartei">Liste B</td>'
    '<td class="amname">Mitglied</td><td data-label="Beginn" class="mgadat"></td>'
    '<td data-label="Ende" class="mgedat">13.10.2022</td><td class="mgadat">bis 13.10.2022</td></tr>'
    '<tr class="smc-t-r-l"><td class="pename"><a href="pe0051.php?__kpenr=14">Greta Vorher</a></td>'
    '<td class="pepartei">Liste A</td><td class="amname">Mitglied</td><td data-label="Beginn" class="mgadat">'
    '01.11.2021</td><td data-label="Ende" class="mgedat">31.03.2024</td></tr>'
    # Dieselbe Person wie aktuell (kpenr 11), frühere Mitgliedschaft
    '<tr class="smc-t-r-l"><td class="pename"><a href="pe0051.php?__kpenr=11">Erika Mustermann</a></td>'
    '<td class="pepartei">Liste A</td><td class="amname">Stellvertretung</td><td class="mgadat"></td>'
    '<td class="mgedat">31.05.2023</td></tr>'
    # Beendet ohne Ende: nicht als aktuelle Mitgliedschaft
    '<tr class="smc-t-r-l"><td class="pename">Otto Ohnedatum</td><td class="pepartei">Liste B</td>'
    '<td class="amname">Mitglied</td><td class="mgadat"></td><td class="mgedat"></td></tr>'
    "</tbody></table>",
    title="Gremium",
)


def calendar_row(day: int, content: str, termin: str = "") -> str:
    return (
        f'<tr><td class="smc_fct_day"><span class="weekday">{day}</span></td>'
        f'<td class="smc-t-cl991 silink" data-label="Sitzung">{content}</td>'
        f'<td class="pagel pagel3" data-label="Mandant"></td><td class="sitermin">{termin}</td>'
        '<td class="sidocs"></td></tr>'
    )


def termin(ksinr: int, day: int, name: str, uid: str | None = None) -> str:
    return (
        f'<a href="yvcs.php?key={ksinr}&amp;dstart=202611{day:02d}T143000&amp;dend=202611{day:02d}T143100'
        f'&amp;uid={uid or f"si{ksinr}"}&amp;smcid=1&amp;location=Kreishaus&amp;text={name}" '
        f'title="Termin übernehmen: {day:02d}.11.2026 {name}" class="btn">'
        '<i class="smc smc-calendar-add"></i></a>'
    )


def announced(name: str) -> str:
    return (
        f'<div class="smc-el-h">{name}<!--SMCINFO:si.bi.0.0.1.1.2.1.1 --></div>'
        '<ul class="list-inline smc-detail-list"><li class="list-inline-item">14:30 Uhr</li>'
        '<li class="list-inline-item">Musterheide,  Kreishaus, großer Sitzungssaal</li></ul>'
    )


KALENDER = page(
    '<table class="table smc-table smctablesitzungen"><tbody>'
    + calendar_row(1, "")
    # Mit veröffentlichter Tagesordnung
    + calendar_row(
        2,
        '<div class="smc-el-h"><a href="si0057.php?__ksinr=5182" '
        'title="Details anzeigen: Rat der Samtgemeinde Musterheide 02.11.2026">'
        "Rat der Samtgemeinde Musterheide</a></div>"
        '<ul class="list-inline smc-detail-list"><li class="list-inline-item">09:00 Uhr</li>'
        '<li class="list-inline-item">Rathaus, Ratssaal</li></ul>',
        termin(5182, 2, "Rat der Samtgemeinde Musterheide"),
    )
    # Angekündigt, Tagesordnung noch nicht veröffentlicht: Nummer nur im Termin-Link
    + calendar_row(11, announced("Samtgemeindeausschuss"), termin(5214, 11, "Samtgemeindeausschuss"))
    # Vergangene nichtöffentliche Sitzung ohne jede Kennung
    + calendar_row(12, announced("Kreisausschuss"))
    # Termin-Link eines anderen Vorgangs (uid passt nicht zur Nummer)
    + calendar_row(13, announced("Fraktionstermin"), termin(77, 13, "Fraktionstermin", uid="te77"))
    + "</tbody></table>",
    title="Kalender",
)

TAGESORDNUNG = page(
    '<h1 class="smc_h1">Rat der Samtgemeinde Musterheide</h1>'
    '<table id="smc_page_si0057_contenttable1" class="table smc-table smctablesitzung"><tbody>'
    # Keine Abschnittsüberschrift ("Öffentlicher Teil:"): nur das Kennzeichen am Punkt
    '<tr><td class="tofnum"><span class="badge">&Ouml; 1</span></td><td class="tobetr">'
    '<div class="smc-card-header-title-simple">Eröffnung der Sitzung</div></td></tr>'
    '<tr><td class="tofnum"><span class="badge">&Ouml; 2</span></td><td class="tolink">'
    '<div class="smc-card-header-title-simple">Satzung über die Straßenreinigung</div>'
    '<a href="vo0050.php?__kvonr=77">SG/1/2026</a></td></tr>'
    '<tr><td class="tofnum"><span class="badge">N 1</span></td><td class="tobetr">'
    '<div class="smc-card-header-title-simple">Grundstücksangelegenheit</div></td></tr>'
    "</tbody></table>",
    title="Sitzung",
)

SITZUNG_INFO = page(
    '<div class="smc-table-cell siname">SG/SGR/001/2026</div>'
    '<div class="smc-table-cell sigrname">Rat der Samtgemeinde Musterheide</div>'
    '<div class="smc-table-cell siort">Rathaus, Ratssaal</div><div class="smc-table-cell sidat">02.11.2026</div>'
    '<div class="smc-table-cell yytime">09:00&nbsp;Uhr</div>',
    title="Sitzung",
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_gremium_ohne_mitgliederseite_mit_nummer_aus_der_sitzungsliste() -> None:
    assert parse_organizations(GREMIEN) == [
        {"kgrnr": 1, "name": "Rat der Samtgemeinde Musterheide", "members_page": True},
        {"kgrnr": 8, "name": "Samtgemeindeausschuss", "members_page": False},
    ]


def test_beendete_mitgliedschaften_mit_ende_und_ohne_stimmrecht_der_verwaltung() -> None:
    rows = parse_members(MITGLIEDER)
    members = {m["name"]: m for m in rows if not (m["kpenr"] == 11 and m["end_date"])}
    assert set(members) == {"Erika Mustermann", "Max Beispiel", "Paula Protokoll", "Hans Frueher", "Greta Vorher"}
    aktuell = members["Erika Mustermann"]
    assert (aktuell["voting_right"], aktuell["start_date"], aktuell["end_date"]) == (True, None, None)
    # Hinzugezogene und Verwaltung stimmen nicht mit
    assert members["Max Beispiel"]["voting_right"] is False
    assert members["Paula Protokoll"]["voting_right"] is False
    # Beendete Mitgliedschaften: Beginn und Ende aus den Spalten "von"/"bis", kein Abschnitt der ersten Tabelle
    assert members["Hans Frueher"]["end_date"] == "2022-10-13"
    assert members["Hans Frueher"]["start_date"] is None
    assert members["Hans Frueher"]["section"] is None
    assert (members["Greta Vorher"]["start_date"], members["Greta Vorher"]["end_date"]) == ("2021-11-01", "2024-03-31")
    assert members["Greta Vorher"]["kpenr"] == 14


def test_beendete_mitgliedschaft_ueberschreibt_nicht_die_aktuelle() -> None:
    rows = parse_members(MITGLIEDER)
    keys = dict(zip(((r["name"], r["end_date"]) for r in rows), membership_keys(rows), strict=True))
    # Aktuelle Zeile wie bisher nach der Person, beendete derselben Person mit dem Ende als Zusatz
    assert keys[("Erika Mustermann", None)] == "11"
    assert keys[("Erika Mustermann", "2023-05-31")] == "11-bis-2023-05-31"
    # Ohne aktuelle Zeile derselben Person bleibt auch die beendete bei der Person
    assert keys[("Greta Vorher", "2024-03-31")] == "14"
    assert keys[("Hans Frueher", "2022-10-13")] == "hans-frueher"
    assert len(set(keys.values())) == len(rows)
    # Weitere Dopplung zählt weiter
    doppelt = [{"kpenr": 5, "name": "A", "end_date": None}, {"kpenr": 5, "name": "A", "end_date": None}]
    assert membership_keys(doppelt) == ["5", "5-2"]


def test_gegenprobe_mandant_liest_angekuendigte_sitzungen() -> None:
    html = KALENDER.replace(
        '<td class="pagel pagel3" data-label="Mandant"></td><td class="sitermin"><a href="yvcs.php?key=5214',
        '<td class="pagel pagel3" data-label="Mandant">Gemeinde Musterdorf</td>'
        '<td class="sitermin"><a href="yvcs.php?key=5214',
    )
    assert parse_calendar_mandanten(html) == {5214: "Gemeinde Musterdorf"}


def test_fehlerseite_von_sessionnet_wird_erkannt() -> None:
    # Die Fehlerseite trägt das SessionNet-Layout und galt deshalb als lesbare Seite
    assert is_sessionnet_page(ERROR_PAGE)
    assert sessionnet_error_code(ERROR_PAGE) == "1104"
    assert sessionnet_error_code(GREMIEN) is None


def test_angekuendigte_sitzung_aus_dem_termin_link() -> None:
    stubs = {s.ksinr: s for s in parse_calendar(KALENDER)}
    assert set(stubs) == {5182, 5214}
    assert stubs[5182].agenda_linked is True
    angekuendigt = stubs[5214]
    assert angekuendigt.agenda_linked is False
    assert (angekuendigt.name, angekuendigt.committee) == ("Samtgemeindeausschuss", "Samtgemeindeausschuss")
    assert (angekuendigt.date, angekuendigt.time_start, angekuendigt.time_end) == ("11.11.2026", "14:30", None)
    assert angekuendigt.location == "Musterheide, Kreishaus, großer Sitzungssaal"


def test_oeffentlich_aus_dem_kennzeichen_ohne_abschnittsueberschrift() -> None:
    rows = parse_meeting_agenda(TAGESORDNUNG).rows
    assert [(r.number, r.public) for r in rows] == [("1", True), ("2", True), ("1", False)]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class Fetcher:
    """PoliteFetcher-Ersatz: liefert die Ausschnitte je Seite, merkt sich die Anfragen."""

    source_name = "fixture"

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list[str] = []
        self.last_status: int | None = None

    async def fetch_text(self, url: str) -> str | None:
        self.requests.append(url)
        parsed = urlparse(url)
        name = parsed.path.rsplit("/", 1)[-1].split(".")[0]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        key = next((f"{name}_{query[p]}" for p in ("__kgrnr", "__ksinr", "__kvonr") if p in query), name)
        html = self.pages.get(key)
        self.last_status = 200 if html is not None else 404
        return html


def make_adapter(pages: dict[str, str]) -> tuple[SessionNetAdapter, Fetcher]:
    fetcher = Fetcher(pages)
    config = ScraperConfig(base_url=BASE_URL, body_name="Samtgemeinde Musterheide", variant="php")
    return SessionNetAdapter(config, fetcher), fetcher  # type: ignore[arg-type]


async def crawl(adapter: SessionNetAdapter) -> dict[str, dict[str, dict[str, Any]]]:
    collected: dict[str, dict[str, dict[str, Any]]] = {}
    async for entity_type, items in adapter.iter_entities(NOVEMBER, full=True):
        for item in items:
            collected.setdefault(entity_type, {})[item["id"]] = item
    return collected


def url(name: str, **params: Any) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE_URL}{name}.php?{query}"


PAGES = {
    "gr0040": GREMIEN,
    "kp0040_1": MITGLIEDER,
    "si0040": KALENDER,
    "si0057_5182": TAGESORDNUNG,
    "si0050_5182": SITZUNG_INFO,
    # Vorlage nicht öffentlich: Fehlerseite statt Vorlage
    "vo0050_77": ERROR_PAGE,
}


async def test_adapter_liest_die_luecken_ohne_zusaetzliche_abrufe() -> None:
    adapter, fetcher = make_adapter(PAGES)
    collected = await crawl(adapter)

    # Gremium ohne Mitgliederseite: angelegt, Mitgliederliste nicht angefragt
    ausschuss = url("kp0040", __kgrnr=8)
    assert ausschuss in collected["organization"]
    assert "web" not in collected["organization"][ausschuss]
    assert not any("__kgrnr=8" in request for request in fetcher.requests)

    # Beendete Mitgliedschaften mit Ende, Verwaltung ohne Stimmrecht
    memberships = {m["person"].rsplit("/", 1)[-1]: m for m in collected["membership"].values()}
    assert memberships["hans-frueher"]["endDate"] == "2022-10-13"
    assert "startDate" not in memberships["hans-frueher"]
    greta = next(m for m in collected["membership"].values() if m["person"].endswith("__kpenr=14"))
    assert (greta["startDate"], greta["endDate"]) == ("2021-11-01", "2024-03-31")
    verwaltung = next(m for m in collected["membership"].values() if m["person"].endswith("__kpenr=13"))
    assert verwaltung["votingRight"] is False
    aktuell = collected["membership"][f"{url('kp0040', __kgrnr=1)}#membership/11"]
    assert "endDate" not in aktuell
    frueher = collected["membership"][f"{url('kp0040', __kgrnr=1)}#membership/11-bis-2023-05-31"]
    assert (frueher["person"], frueher["endDate"]) == (aktuell["person"], "2023-05-31")

    # Angekündigte Sitzung aus dem Kalender, ohne Abruf ihrer Seiten, mit Gremium über den Namen
    angekuendigt = collected["meeting"][url("si0057", __ksinr=5214)]
    assert angekuendigt["start"] == "2026-11-11T14:30:00+01:00"
    assert angekuendigt["organization"] == [ausschuss]
    assert angekuendigt["location"]["description"] == "Musterheide, Kreishaus, großer Sitzungssaal"
    assert "agendaItem" not in angekuendigt
    assert not any("__ksinr=5214" in request for request in fetcher.requests)
    assert set(collected["meeting"]) == {url("si0057", __ksinr=5182), url("si0057", __ksinr=5214)}

    # Nicht öffentliche Vorlage: keine Vorlage "SessionNet Fehlermeldung", Lauf trotzdem vollständig
    assert "paper" not in collected
    assert adapter.stats.gone_pages == adapter.stats.failed_pages == 1
    # Die nicht öffentliche Vorlage zählt nicht gegen die Parse-Quote
    assert adapter.stats.parse_failures == 0
    assert adapter.stats.parse_quota == 1.0
    assert adapter.stats.complete

    processor = OParlProcessor()
    body_id = adapter.build_body()["id"]
    for items in collected.values():
        for item in items.values():
            assert processor.process(item, body_id) is not None


async def test_nicht_oeffentliche_sitzungsseite_ergibt_sitzung_aus_dem_kalender() -> None:
    pages = {**PAGES, "si0057_5182": ERROR_PAGE}
    adapter, fetcher = make_adapter(pages)
    collected = await crawl(adapter)

    sitzung = collected["meeting"][url("si0057", __ksinr=5182)]
    assert sitzung["name"] == "Rat der Samtgemeinde Musterheide"
    assert sitzung["start"] == "2026-11-02T09:00:00+01:00"
    assert "agendaItem" not in sitzung
    # Die Informationsseite fragen wir dann nicht an
    assert not any("si0050" in request for request in fetcher.requests)
    assert adapter.stats.complete
    assert adapter.stats.parse_quota == 1.0


async def test_andere_fehlerseite_bleibt_eine_stoerung() -> None:
    other_error = ERROR_PAGE.replace("Fehlercode: 1104", "Fehlercode: 1001")
    adapter, _ = make_adapter({**PAGES, "vo0050_77": other_error})
    collected = await crawl(adapter)

    assert "paper" not in collected
    assert adapter.stats.failed_pages == 1
    assert adapter.stats.gone_pages == 0
    assert not adapter.stats.complete


async def test_angekuendigte_sitzung_eines_anderen_mandanten_wird_nicht_zugeordnet() -> None:
    toggle = (
        '<ul class="nav nav-pills"><li class="nav-item dropdown"><a aria-label="Mandant auswählen" '
        'title="Mandant auswählen" class="nav-link dropdown-toggle" href="#">Samtgemeinde Musterheide</a>'
        '<div class="dropdown-menu"><a href="gr0040.php?__cpanr=2" class="dropdown-item smcfiltermenumandant">'
        "Gemeinde Musterdorf</a></div></li></ul>"
    )
    gremien = GREMIEN.replace('<table id="smc_page_gr0040', toggle + '<table id="smc_page_gr0040')
    # Die Instanz hat __cpanr nicht beachtet: Die angekündigte Sitzung gehört laut Spalte "Mandant" zur Gemeinde
    kalender = KALENDER.replace(
        '<td class="pagel pagel3" data-label="Mandant"></td><td class="sitermin"><a href="yvcs.php?key=5214',
        '<td class="pagel pagel3" data-label="Mandant">Gemeinde Musterdorf</td>'
        '<td class="sitermin"><a href="yvcs.php?key=5214',
    ).replace(
        '<td class="pagel pagel3" data-label="Mandant"></td><td class="sitermin"><a href="yvcs.php?key=5182',
        '<td class="pagel pagel3" data-label="Mandant">Samtgemeinde Musterheide</td>'
        '<td class="sitermin"><a href="yvcs.php?key=5182',
    )
    fetcher = Fetcher({**PAGES, "gr0040": gremien, "si0040": kalender})
    config = ScraperConfig(
        base_url=BASE_URL, body_name="Samtgemeinde Musterheide", variant="php", bodies=[BodySpec(cpanr=1)]
    )
    adapter = SessionNetAdapter(config, fetcher)  # type: ignore[arg-type]
    collected = await crawl(adapter)

    assert set(collected["meeting"]) == {url("si0057", __ksinr=5182)}
