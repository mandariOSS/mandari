# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergänzung von OParl-Objekten aus den Detailseiten von ALLRIS 4 (Issue #1431).

Die Seiten sind nachgebaut (erfundener Host, erfundene Inhalte) und bilden nur den Aufbau der Detailseiten von
ALLRIS 4 nach, wie ihn die Stadt Herzogenrath im November 2026 auslieferte: Rahmen ``#allris`` mit ``#pageContent``
und ``h1.title``, Grunddaten als ``dl``/``dt.colTitle``/``dd``, Abschnitte als ``div.compFull`` mit
``h2.expandedTitle`` und Textteilen ``div.docPart`` aus Word.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.client.oparl_client import OParlClient, SyncStats
from src.client.oparl_compat import timestamps_unreliable
from src.scrapers.allris4 import parse_agenda_item_page, parse_paper_page, parse_vote, result_code
from src.sync.html_ergaenzung import (
    DEFAULT_PAGES_PER_RUN,
    MARKER_KEY,
    HtmlErgaenzung,
    HtmlErgaenzungConfig,
    detail_page,
)

HOST = "ratsinfo.example.de"
PUBLIC = f"http://{HOST}/public"
OPARL = f"{PUBLIC}/oparl"


def seite(titel: str, grunddaten: dict[str, str], abschnitte: dict[str, list[str]]) -> str:
    """Detailseite im Aufbau von ALLRIS 4."""
    dl = "".join(
        f'<dl class="row"><dt class="colTitle">{name}:</dt><dd>{wert}</dd></dl>' for name, wert in grunddaten.items()
    )
    kaesten = []
    for nummer, (ueberschrift, absaetze) in enumerate(abschnitte.items()):
        text = "".join(
            f'<p style="margin-top:0pt"><span style="font-family:Arial">{absatz}</span></p>' for absatz in absaetze
        )
        teil = f'<div class="docPart readOnly" id="htm{1000 + nummer}"><div>{text}</div></div>' if absaetze else ""
        kaesten.append(
            f'<div class="compFull roundedBorder"><div class="expcol"><a href="#">x</a></div>'
            f'<div id="id{nummer}"><h2 class="expandedTitle">{ueberschrift}</h2>{teil}</div></div>'
        )
    return (
        '<!DOCTYPE html><html><head><script id="wicket-ajax-base-url">var x;</script></head><body>'
        '<div id="allris"><div id="cookieDialog"><h2 id="cookieConsent">Cookies</h2></div>'
        '<main id="pageContent"><div id="readOut"><div><div class="header">'
        f'<h1 class="title">{titel}</h1></div>'
        f'<div class="compFull roundedBorder"><h2>Grunddaten</h2>{dl}</div>'
        f'<div id="mainContent">{"".join(kaesten)}</div>'
        "</div></div></main></div></body></html>"
    )


TOP_SEITE = seite(
    "29.09.2026 - 4 Haushalt der Region",
    {"TOP": "Ö 4", "Gremium": "Hauptausschuss", "Beschluss": "geändert beschlossen"},
    {
        "Wortprotokoll": ["Frau A. erläutert die Vorlage.", "Herr B. beantragt eine Ergänzung um Nr. 5."],
        "Beschluss": ["Beschluss:", "Der Ausschuss empfiehlt dem Rat:", "1. Das Benehmen wird nicht hergestellt."],
        "Abstimmungsergebnis": ["Abstimmungsergebnis:", "Ja-Stimmen: 12", "Nein-Stimmen: 3", "Enthaltungen: 1"],
        "Anlagen zur Vorlage": [],
    },
)
VORLAGE_SEITE = seite(
    "Sitzungsvorlage - V/2026/337",
    {"Betreff": "Haushalt der Region", "Vorlageart": "Sitzungsvorlage"},
    {
        "Beratungsfolge": [],
        "Beschlussvorschlag": ["Der Rat beschließt:", "Das Benehmen wird nicht hergestellt."],
        "Finanz. Auswirkung": ["Gesamtkosten", "ja nein"],
        "Sachverhalt": ["Die Region hat das Verfahren eingeleitet.", "Die Umlage steigt deutlich."],
        "Anlagen": [],
    },
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParser:
    def test_tagesordnungspunkt(self):
        felder = parse_agenda_item_page(TOP_SEITE, "geändert beschlossen")
        assert felder is not None
        assert felder["resolutionText"] == (
            "Der Ausschuss empfiehlt dem Rat:\n1. Das Benehmen wird nicht hergestellt.\n\n"
            "Abstimmungsergebnis:\nJa-Stimmen: 12\nNein-Stimmen: 3\nEnthaltungen: 1"
        )
        assert felder["mandari:vote"] == {"method": "summary", "yes": 12, "no": 3, "abstain": 1, "result": "approved"}
        assert felder["mandari:minutes"] == "Frau A. erläutert die Vorlage.\nHerr B. beantragt eine Ergänzung um Nr. 5."

    def test_beschlussart_von_der_seite_ohne_result(self):
        # In Sitzungen eingebettete Punkte tragen bei ALLRIS 4 kein result: Beschlussart aus den Grunddaten
        felder = parse_agenda_item_page(TOP_SEITE)
        assert felder is not None
        assert felder["mandari:vote"]["result"] == "approved"

    def test_einstimmig_ohne_zahl(self):
        assert parse_vote(["Ja-Stimmen: Einstimmig"], "ungeändert beschlossen") == {
            "method": "summary",
            "no": 0,
            "abstain": 0,
            "result": "approved",
        }
        assert parse_vote(["Ja- Stimmen: 15", "Nein- Stimmen: 0", "Enthaltungen: 2"]) == {
            "method": "summary",
            "yes": 15,
            "no": 0,
            "abstain": 2,
        }
        assert parse_vote(["Die Abstimmung wurde vertagt."]) is None

    def test_beschlussarten(self):
        assert result_code("zur Kenntnis genommen") == "noted"
        assert result_code("zustimmend zur Kenntnis genommen") == "noted"
        assert result_code("abgelehnt") == "rejected"
        assert result_code("vertagt") == "deferred"
        assert result_code("ungeändert beschlossen") == "approved"
        assert result_code("ohne Votum") is None
        assert result_code(None) is None

    def test_punkt_ohne_abschnitte(self):
        felder = parse_agenda_item_page(seite("15.06.2026 - 1 Eröffnung", {"TOP": "Ö 1"}, {}))
        assert felder == {}

    def test_vorlage(self):
        felder = parse_paper_page(VORLAGE_SEITE)
        assert felder == {
            "mandari:proposal": "Der Rat beschließt:\nDas Benehmen wird nicht hergestellt.",
            "mandari:explanation": "Die Region hat das Verfahren eingeleitet.\nDie Umlage steigt deutlich.",
        }

    def test_fremde_seite(self):
        assert parse_agenda_item_page("<html><body><h1>Wartung</h1></body></html>") is None
        assert parse_paper_page("<html><body><div id='allris'>Fehler</div></body></html>") is None


# ---------------------------------------------------------------------------
# Konfiguration und Seitenauswahl
# ---------------------------------------------------------------------------


class TestKonfiguration:
    def test_parse(self):
        assert HtmlErgaenzungConfig.parse(None) is None
        assert HtmlErgaenzungConfig.parse({}) is None
        assert HtmlErgaenzungConfig.parse({"html_ergaenzung": "sessionnet"}) is None
        assert HtmlErgaenzungConfig.parse({"html_ergaenzung": "ALLRIS4"}) == HtmlErgaenzungConfig(
            "allris4", DEFAULT_PAGES_PER_RUN
        )
        config = HtmlErgaenzungConfig.parse({"html_ergaenzung": "allris4", "html_ergaenzung_je_lauf": 50})
        assert config == HtmlErgaenzungConfig("allris4", 50)
        for falsch in (True, -1, "100", 10**9):
            config = HtmlErgaenzungConfig.parse({"html_ergaenzung": "allris4", "html_ergaenzung_je_lauf": falsch})
            assert config is not None and config.pages_per_run == DEFAULT_PAGES_PER_RUN

    def test_nur_bekannte_seiten_auf_dem_host_der_quelle(self):
        hosts = [HOST]
        assert detail_page("agendaitem", f"{PUBLIC}/to020?TOLFDNR=1004622", hosts)
        assert detail_page("paper", f"https://{HOST}/public/vo020?VOLFDNR=1002035", hosts)
        assert detail_page("agendaitem", f"{PUBLIC}/vo020?VOLFDNR=1", hosts) is None
        assert detail_page("agendaitem", f"{PUBLIC}/to020?TOLFDNR=abc", hosts) is None
        assert detail_page("agendaitem", "http://andere.example.de/public/to020?TOLFDNR=1", hosts) is None
        assert detail_page("meeting", f"{PUBLIC}/to010?SILFDNR=1", hosts) is None
        assert detail_page("paper", None, hosts) is None


# ---------------------------------------------------------------------------
# Ergänzung im Abgleich
# ---------------------------------------------------------------------------


@dataclass
class Antwort:
    status_code: int = 200
    error: str | None = None
    text: str | None = None


class Quelle:
    """Seiten und Bestand für die Ergänzung."""

    def __init__(self) -> None:
        self.seiten: dict[str, Antwort] = {}
        self.abrufe: list[str] = []
        self.bestand: dict[str, dict[str, Any]] = {}

    async def fetch_page(self, url: str) -> Antwort:
        self.abrufe.append(url)
        return self.seiten.get(url, Antwort(404, "Not found"))

    async def load_stored(self, entity_type: str, external_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {key: self.bestand[key] for key in external_ids if key in self.bestand}

    def speichern(self, objekt: dict[str, Any]) -> None:
        """Wie der Upsert: Rohdatensatz mit Vermerk landet im Bestand."""
        if MARKER_KEY in objekt:
            self.bestand[objekt["id"]] = dict(objekt)


def ergaenzung(quelle: Quelle, je_lauf: int = DEFAULT_PAGES_PER_RUN) -> HtmlErgaenzung:
    return HtmlErgaenzung(
        HtmlErgaenzungConfig("allris4", je_lauf),
        hosts=[HOST],
        fetch_page=quelle.fetch_page,
        load_stored=quelle.load_stored,
    )


def punkt(nummer: int, **felder: Any) -> dict[str, Any]:
    daten: dict[str, Any] = {
        "id": f"{OPARL}/agendaItems?id={nummer}",
        "type": "https://schema.oparl.org/1.1/AgendaItem",
        "name": "Haushalt der Region",
        "result": "geändert beschlossen",
        "web": f"{PUBLIC}/to020?TOLFDNR={nummer}",
        "created": "2026-09-08T10:32:59+02:00",
        "modified": "2026-10-05T07:16:50+02:00",
    }
    daten.update(felder)
    return daten


class TestErgaenzung:
    async def test_laedt_einmal_und_uebernimmt_danach_aus_dem_bestand(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(text=TOP_SEITE)
        lauf = ergaenzung(quelle)

        erster = punkt(1)
        await lauf.ergaenze("agendaitem", erster)
        assert erster["mandari:vote"]["yes"] == 12
        assert erster[MARKER_KEY] == {
            "art": "allris4",
            "seite": f"{PUBLIC}/to020?TOLFDNR=1",
            "stand": "2026-10-05T07:16:50+02:00",
            "felder": ["resolutionText", "mandari:vote", "mandari:minutes"],
        }
        quelle.speichern(erster)

        # Derselbe Punkt eingebettet in seiner Sitzung: kein zweiter Abruf, gleicher Rohdatensatz
        sitzung = {"id": f"{OPARL}/meetings?id=9", "agendaItem": [punkt(1, result=None)]}
        await lauf.ergaenze("meeting", sitzung)
        eingebettet = sitzung["agendaItem"][0]
        assert quelle.abrufe == [f"{PUBLIC}/to020?TOLFDNR=1"]
        for key in ("resolutionText", "mandari:vote", "mandari:minutes", MARKER_KEY):
            assert eingebettet[key] == erster[key]
        assert lauf.stats.as_dict() == {
            "geladen": 1,
            "uebernommen": 1,
            "fehlgeschlagen": 0,
            "verschoben": 0,
            "ohne_seite": 0,
        }

    async def test_aenderung_laedt_neu_unzuverlaessiger_stempel_nicht(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(text=TOP_SEITE)
        lauf = ergaenzung(quelle)
        objekt = punkt(1)
        await lauf.ergaenze("agendaitem", objekt)
        quelle.speichern(objekt)

        # ALLRIS 4 stempelt ältere Punkte mit der Uhrzeit des Abrufs: keine Änderung
        jetzt = datetime.now(UTC).replace(microsecond=0).isoformat()
        await lauf.ergaenze("agendaitem", punkt(1, created=jetzt, modified=jetzt))
        assert len(quelle.abrufe) == 1

        # Echte Änderung (anderer, verlässlicher Stempel): neu laden
        geaendert = punkt(1, modified="2026-11-20T08:00:00+01:00")
        await lauf.ergaenze("agendaitem", geaendert)
        assert len(quelle.abrufe) == 2
        assert geaendert[MARKER_KEY]["stand"] == "2026-11-20T08:00:00+01:00"

    async def test_felder_der_quelle_haben_vorrang(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(text=TOP_SEITE)
        objekt = punkt(1, resolutionText="Text aus OParl")
        await ergaenzung(quelle).ergaenze("agendaitem", objekt)
        assert objekt["resolutionText"] == "Text aus OParl"
        assert objekt[MARKER_KEY]["felder"] == ["mandari:vote", "mandari:minutes"]

    async def test_grenze_je_lauf_und_fehler(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(text=TOP_SEITE)
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=2"] = Antwort(500, "HTTP 500")
        lauf = ergaenzung(quelle, je_lauf=2)
        objekte = [punkt(1), punkt(2), punkt(3)]
        for objekt in objekte:
            await lauf.ergaenze("agendaitem", objekt)
        assert quelle.abrufe == [f"{PUBLIC}/to020?TOLFDNR=1", f"{PUBLIC}/to020?TOLFDNR=2"]
        assert MARKER_KEY in objekte[0]
        assert MARKER_KEY not in objekte[1] and MARKER_KEY not in objekte[2]
        assert (lauf.stats.loaded, lauf.stats.failed, lauf.stats.deferred) == (1, 1, 1)

    async def test_gespeicherte_ergaenzung_bleibt_bei_fehler_und_grenze(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(text=TOP_SEITE)
        objekt = punkt(1)
        await ergaenzung(quelle).ergaenze("agendaitem", objekt)
        quelle.speichern(objekt)

        # Geändert, aber die Seite ist gestört: die bisherige Ergänzung bleibt
        quelle.seiten[f"{PUBLIC}/to020?TOLFDNR=1"] = Antwort(503, "HTTP 503")
        geaendert = punkt(1, modified="2026-11-20T08:00:00+01:00")
        await ergaenzung(quelle).ergaenze("agendaitem", geaendert)
        assert geaendert["mandari:minutes"] == objekt["mandari:minutes"]
        assert geaendert[MARKER_KEY]["stand"] == "2026-10-05T07:16:50+02:00"  # nächster Lauf versucht es wieder

        # Grenze erreicht: ebenso
        ohne_kontingent = punkt(1, modified="2026-11-21T08:00:00+01:00")
        await ergaenzung(quelle, je_lauf=0).ergaenze("agendaitem", ohne_kontingent)
        assert ohne_kontingent["resolutionText"] == objekt["resolutionText"]

    async def test_vorlage_und_fremde_seite(self):
        quelle = Quelle()
        quelle.seiten[f"{PUBLIC}/vo020?VOLFDNR=7"] = Antwort(text=VORLAGE_SEITE)
        quelle.seiten[f"{PUBLIC}/vo020?VOLFDNR=8"] = Antwort(text="<html><body>Wartungsarbeiten</body></html>")
        lauf = ergaenzung(quelle)
        vorlage = {"id": f"{OPARL}/papers?id=7", "web": f"{PUBLIC}/vo020?VOLFDNR=7", "modified": "2026-10-09"}
        kaputt = {"id": f"{OPARL}/papers?id=8", "web": f"{PUBLIC}/vo020?VOLFDNR=8", "modified": "2026-10-09"}
        ohne_web = {"id": f"{OPARL}/papers?id=9", "name": "V/2025/477-E01"}
        for objekt in (vorlage, kaputt, ohne_web):
            await lauf.ergaenze("paper", objekt)
        assert vorlage["mandari:proposal"].startswith("Der Rat beschließt:")
        assert MARKER_KEY not in kaputt and MARKER_KEY not in ohne_web
        assert (lauf.stats.loaded, lauf.stats.failed, lauf.stats.skipped) == (1, 1, 1)

    async def test_andere_objektarten_bleiben_unberuehrt(self):
        quelle = Quelle()
        person = {"id": f"{OPARL}/persons?id=1", "web": f"{PUBLIC}/kp020?KPLFDNR=1"}
        await ergaenzung(quelle).ergaenze("person", person)
        assert quelle.abrufe == [] and MARKER_KEY not in person


# ---------------------------------------------------------------------------
# Stempel des Abrufs (ALLRIS 4)
# ---------------------------------------------------------------------------


class TestStempelDesAbrufs:
    def test_gleiche_stempel_zur_zeit_des_abrufs(self):
        jetzt = datetime(2026, 11, 16, 16, 4, 27, tzinfo=UTC)
        stempel = "2026-11-16T17:04:27+01:00"
        assert timestamps_unreliable({"created": stempel, "modified": stempel}, now=jetzt)
        assert timestamps_unreliable({"created": stempel, "modified": stempel}, now=jetzt + timedelta(minutes=29))

    def test_echte_stempel_bleiben_verlaesslich(self):
        jetzt = datetime(2026, 11, 16, 16, 4, 27, tzinfo=UTC)
        alt = "2026-03-05T15:14:35+01:00"
        assert not timestamps_unreliable({"created": alt, "modified": alt}, now=jetzt)
        assert not timestamps_unreliable(
            {"created": "2026-11-16T17:00:00+01:00", "modified": "2026-11-16T17:04:27+01:00"}, now=jetzt
        )
        assert not timestamps_unreliable({"created": "2026-11-16T17:04:27", "modified": "2026-11-16T17:04:27"})


# ---------------------------------------------------------------------------
# Client: Seitenabruf und Weiterleitung auf https
# ---------------------------------------------------------------------------


def client_mit(handler: Any) -> OParlClient:
    client = OParlClient(max_concurrent=1, wait_time=0)
    client._semaphore = asyncio.Semaphore(1)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.stats = SyncStats()
    client._circuit_breakers = {}
    return client


class TestClient:
    async def test_fetch_page_liefert_text(self):
        gesehen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            gesehen.append(request)
            return httpx.Response(200, text=TOP_SEITE, headers={"Content-Type": "text/html;charset=UTF-8"})

        client = client_mit(handler)
        ergebnis = await client.fetch_page(f"https://{HOST}/public/to020?TOLFDNR=1")
        assert ergebnis.error is None and ergebnis.data is None
        assert ergebnis.text == TOP_SEITE
        assert "text/html" in gesehen[0].headers["accept"]

    async def test_nach_weiterleitung_auf_https_gleich_per_https(self):
        gesehen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            gesehen.append(str(request.url))
            if request.url.scheme == "http":
                return httpx.Response(302, headers={"Location": str(request.url.copy_with(scheme="https"))})
            return httpx.Response(200, json={"id": str(request.url).replace("https://", "http://"), "data": []})

        client = client_mit(handler)
        await client.fetch(f"{OPARL}/papers?body=1&page=2")
        await client.fetch(f"{OPARL}/papers?body=1&page=3")
        assert gesehen == [
            f"{OPARL}/papers?body=1&page=2",
            f"https://{HOST}/public/oparl/papers?body=1&page=2",
            f"https://{HOST}/public/oparl/papers?body=1&page=3",
        ]
        assert client.https_upgrade("http://anderer.example.de/x") == "http://anderer.example.de/x"

    async def test_weiterleitung_auf_andere_adresse_merkt_nichts(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.scheme == "http":
                return httpx.Response(302, headers={"Location": f"https://{HOST}/neu/system"})
            return httpx.Response(200, json={"id": "x"})

        client = client_mit(handler)
        await client.fetch(f"http://{HOST}/alt/system")
        assert client.https_hosts == set()


# ---------------------------------------------------------------------------
# Bestand: gespeicherte Ergänzung lesen
# ---------------------------------------------------------------------------


class _Sitzung:
    """Fängt die Abfrage ab und liefert vorbereitete Zeilen."""

    def __init__(self, zeilen: list[tuple[Any, ...]]) -> None:
        self.zeilen = zeilen
        self.abfragen: list[Any] = []

    async def __aenter__(self) -> _Sitzung:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        self.abfragen.append(stmt)
        zeilen = self.zeilen

        class _Ergebnis:
            def all(self) -> list[tuple[Any, ...]]:
                return zeilen

        return _Ergebnis()


class TestBestand:
    async def test_abfrage_und_zeilen(self):
        from sqlalchemy.dialects import postgresql

        from src.storage.database import DatabaseStorage
        from src.sync.html_ergaenzung import AGENDA_ITEM_FIELDS

        vermerk = {"art": "allris4", "seite": "x", "stand": "s", "felder": ["resolutionText"]}
        sitzung = _Sitzung([(f"{OPARL}/agendaItems?id=1", vermerk, "Beschluss", None, None)])
        speicher = DatabaseStorage.__new__(DatabaseStorage)
        speicher.get_session = lambda: sitzung  # type: ignore[method-assign]

        gespeichert = await speicher.get_html_ergaenzung(
            "agendaitem", [f"{OPARL}/agendaItems?id=1", f"{OPARL}/agendaItems?id=2"], AGENDA_ITEM_FIELDS, MARKER_KEY
        )
        assert gespeichert == {f"{OPARL}/agendaItems?id=1": {MARKER_KEY: vermerk, "resolutionText": "Beschluss"}}
        sql = str(sitzung.abfragen[0].compile(dialect=postgresql.dialect()))
        assert "oparl_agenda_items.raw_json ? " in sql
        assert sql.count("oparl_agenda_items.raw_json[") == 4
        assert await speicher.get_html_ergaenzung("unbekannt", ["x"], AGENDA_ITEM_FIELDS, MARKER_KEY) == {}
