# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der robots.txt-Zwischenspeicher hat ein Budget und verdrängt, was am längsten nicht gebraucht wurde.

Hostnamen kommen auch aus den Daten der Quellen (Adressen von Dateien, Ziele von Weiterleitungen); ihre Zahl ist
nicht begrenzt. Der Zwischenspeicher zählt deshalb den geschätzten Speicher aller Einträge, Regeln und Musterlängen
eingeschlossen, und hält ihn unter dem Budget. Eine große robots.txt wird in einem eigenen Thread ausgewertet.

Keine Abrufe fremder Server: Die Antworten kommen aus ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mandari_oparl.robots import KIND_API, MAX_BYTES, RobotsTxt

from src.client.robots import (
    AGENT_COST_BYTES,
    ENTRY_COST_BYTES,
    GROUP_COST_BYTES,
    RULE_COST_BYTES,
    RobotsGate,
)

UA = "x/1"
#: Die eine Gruppe „User-agent: *“ der Beispieldateien
GRUPPE = GROUP_COST_BYTES + AGENT_COST_BYTES + 2 * len("*")


def _server(inhalte: dict[str, str], gesehen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request.url.host)
        return httpx.Response(200, text=inhalte.get(request.url.host, "User-agent: *\nDisallow: /intern\n"))

    return httpx.MockTransport(handler)


def _regeln(anzahl: int, laenge: int = 10) -> str:
    return "User-agent: *\n" + "".join(f"Disallow: /{nummer:06d}{'x' * laenge}\n" for nummer in range(anzahl))


async def _pruefen(gate: RobotsGate, client: httpx.AsyncClient, host: str, pfad: str = "/a") -> bool:
    return (await gate.decide(client, f"https://{host}{pfad}", user_agent=UA, kind=KIND_API)).allowed


async def test_mehr_hostnamen_als_das_budget_verdraengen_die_aeltesten(echte_robots: None) -> None:
    je_eintrag = ENTRY_COST_BYTES + GRUPPE + 100 * (RULE_COST_BYTES + 2 * len("/000000xxxxxxxxxx"))
    gate = RobotsGate(budget=10 * je_eintrag)
    gesehen: list[str] = []
    hosts = [f"h{nummer}.example.de" for nummer in range(50)]

    async with httpx.AsyncClient(transport=_server(dict.fromkeys(hosts, _regeln(100)), gesehen)) as client:
        for host in hosts:
            assert await _pruefen(gate, client, host)
            assert gate._cost <= gate._budget
            assert len(gate._entries) <= 10 and len(gate._locks) <= 10

        assert len(gate._entries) == 10
        assert sum(eintrag.cost for eintrag in gate._entries.values()) == gate._cost
        lebende_regeln = sum(len(regeln) for e in gate._entries.values() for _, regeln in e.robots.groups)
        assert lebende_regeln == 10 * 100
        # Die zuletzt gebrauchten bleiben, die ältesten sind fort und werden beim nächsten Abruf neu geladen
        assert [schluessel.split()[0] for schluessel in gate._entries] == hosts[-10:]
        abrufe = len(gesehen)
        assert not await _pruefen(gate, client, hosts[-1], "/000007xxxxxxxxxx")
        assert len(gesehen) == abrufe, "aus dem Zwischenspeicher"
        assert await _pruefen(gate, client, hosts[0])
        assert len(gesehen) == abrufe + 1, "verdrängt, also neu geladen"


async def test_gebrauchte_eintraege_bleiben_vor_ungebrauchten(echte_robots: None) -> None:
    gate = RobotsGate(budget=3 * (ENTRY_COST_BYTES + GRUPPE + RULE_COST_BYTES + 2 * len("/intern")))
    gesehen: list[str] = []

    async with httpx.AsyncClient(transport=_server({}, gesehen)) as client:
        for host in ("a.example.de", "b.example.de", "c.example.de"):
            await _pruefen(gate, client, host)
        await _pruefen(gate, client, "a.example.de")  # a ist wieder der jüngste
        await _pruefen(gate, client, "d.example.de")

    assert [schluessel.split()[0] for schluessel in gate._entries] == ["c.example.de", "a.example.de", "d.example.de"]
    assert gesehen == ["a.example.de", "b.example.de", "c.example.de", "d.example.de"]


async def test_wenige_sehr_lange_regeln_zaehlen_mit_ihrer_laenge(echte_robots: None) -> None:
    # Zehn Regeln von je 40.000 Zeichen: nach der Zahl der Regeln fast nichts, nach dem Speicher viel
    lang = _regeln(10, laenge=40_000)
    je_eintrag = ENTRY_COST_BYTES + GRUPPE + 10 * (RULE_COST_BYTES + 2 * 40_007)
    gate = RobotsGate(budget=2 * je_eintrag)
    hosts = [f"lang{nummer}.example.de" for nummer in range(6)]

    async with httpx.AsyncClient(transport=_server(dict.fromkeys(hosts, lang), [])) as client:
        for host in hosts:
            await _pruefen(gate, client, host)
            assert gate._cost <= gate._budget

    assert len(gate._entries) == 2
    musterlaenge = sum(len(r.pattern) for e in gate._entries.values() for _, regeln in e.robots.groups for r in regeln)
    assert musterlaenge == 2 * 10 * 40_007


async def test_gruppen_ohne_regeln_zaehlen_mit(echte_robots: None) -> None:
    # Tausende Gruppen mit leerem „Allow:“: keine einzige Regel, aber Speicher für jede Gruppe und ihren Namen
    gruppen = "".join(f"User-agent: a{nummer}\nAllow:\n" for nummer in range(2000))
    je_eintrag = ENTRY_COST_BYTES + sum(
        GROUP_COST_BYTES + AGENT_COST_BYTES + 2 * len(f"a{nummer}") for nummer in range(2000)
    )
    gate = RobotsGate(budget=2 * je_eintrag)
    hosts = [f"gruppen{nummer}.example.de" for nummer in range(6)]

    async with httpx.AsyncClient(transport=_server(dict.fromkeys(hosts, gruppen), [])) as client:
        for host in hosts:
            assert await _pruefen(gate, client, host)
            assert gate._cost <= gate._budget

    assert len(gate._entries) == 2
    assert all(eintrag.cost == je_eintrag for eintrag in gate._entries.values())
    assert not any(regeln for e in gate._entries.values() for _, regeln in e.robots.groups)


async def test_einzelner_eintrag_ueber_dem_budget_bleibt_bis_zum_naechsten(echte_robots: None) -> None:
    gate = RobotsGate(budget=ENTRY_COST_BYTES)
    gesehen: list[str] = []

    async with httpx.AsyncClient(transport=_server({"gross.example.de": _regeln(50)}, gesehen)) as client:
        # Die Regeln gelten, auch wenn der Eintrag allein das Budget übersteigt: Sperrregeln gehen nie verloren
        assert not await _pruefen(gate, client, "gross.example.de", "/000001xxxxxxxxxx")
        assert len(gate._entries) == 1
        await _pruefen(gate, client, "klein.example.de")

    assert [schluessel.split()[0] for schluessel in gate._entries] == ["klein.example.de"]


def test_vorgabe_haelt_das_budget_ein() -> None:
    gate = RobotsGate(budget=2 * ENTRY_COST_BYTES)
    for nummer in range(5):
        gate.seed(f"https://h{nummer}.example.de/", RobotsTxt.from_response(404), user_agent=UA)

    assert len(gate._entries) == 2 and gate._cost == 2 * ENTRY_COST_BYTES
    gate.clear()
    assert gate._cost == 0


async def test_abgebrochenes_laden_laesst_keine_sperre_liegen(echte_robots: None) -> None:
    gate = RobotsGate()
    haengt = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        haengt.set()
        await asyncio.Event().wait()
        raise AssertionError("nicht erreichbar")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        laden = asyncio.create_task(_pruefen(gate, client, "haengt.example.de"))
        await haengt.wait()
        assert len(gate._locks) == 1
        laden.cancel()
        await asyncio.gather(laden, return_exceptions=True)

    assert gate._locks == {} and len(gate._entries) == 0


async def test_grosse_datei_wird_im_eigenen_thread_ausgewertet(
    echte_robots: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    im_thread: list[int] = []
    echter_thread = asyncio.to_thread

    async def mitzaehlen(funktion: Any, *args: Any) -> Any:
        im_thread.append(len(args[1]))
        return await echter_thread(funktion, *args)

    monkeypatch.setattr(asyncio, "to_thread", mitzaehlen)
    gross = _regeln(30_000)
    assert len(gross) > MAX_BYTES
    gate = RobotsGate()

    async with httpx.AsyncClient(
        transport=_server({"gross.example.de": gross, "klein.example.de": _regeln(3)}, [])
    ) as client:
        # Während die große Datei ausgewertet wird, läuft eine zweite Aufgabe weiter
        ticks = 0

        async def nebenher() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        zaehler = asyncio.create_task(nebenher())
        try:
            assert not await _pruefen(gate, client, "gross.example.de", "/000001xxxxxxxxxx")
        finally:
            zaehler.cancel()
        assert await _pruefen(gate, client, "klein.example.de")

    assert im_thread == [MAX_BYTES], "nur die große Datei, und nur bis zur Auswertungsgrenze gelesen"
    assert ticks > 10
