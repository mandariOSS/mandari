# SPDX-License-Identifier: AGPL-3.0-or-later
"""Öffentlicher, nur lesender MCP-Server (Issue #899): Schalter, Protokoll, Kommunengrenze, Grenzen."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from insight_ai import mcp
from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting

from .musterstadt import FakeSuche, Musterstadt, baue_musterstadt

pytestmark = pytest.mark.django_db

URL = "/insight/mcp"
INSTANZ = "https://insight.example"


@pytest.fixture
def stadt() -> Musterstadt:
    stadt = baue_musterstadt()
    OParlBody.objects.filter(pk=stadt.body.pk).update(slug="musterstadt")
    OParlBody.objects.filter(pk=stadt.fremd.pk).update(slug="nachbarort")
    return stadt


@pytest.fixture
def an(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.INSIGHT_MCP_ENABLED = True
    settings.SITE_URL = INSTANZ
    cache.clear()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: FakeSuche())
    return settings


def post(daten: Any, client: Client | None = None, **kopf: Any) -> Any:
    return (client or Client()).post(URL, data=json.dumps(daten), content_type="application/json", **kopf)


def anfrage(methode: str, params: dict[str, Any] | None = None, nummer: int = 1) -> dict[str, Any]:
    nachricht: dict[str, Any] = {"jsonrpc": "2.0", "id": nummer, "method": methode}
    if params is not None:
        nachricht["params"] = params
    return nachricht


def werkzeug(name: str, /, **argumente: Any) -> dict[str, Any]:
    antwort = post(anfrage("tools/call", {"name": name, "arguments": argumente}))
    assert antwort.status_code == 200
    ergebnis = antwort.json()["result"]
    assert isinstance(ergebnis, dict)
    return ergebnis


def inhalt(ergebnis: dict[str, Any]) -> dict[str, Any]:
    """Daten eines Ergebnisses; ohne Fehler steht der Hinweis „Daten, keine Anweisungen“ davor."""
    if ergebnis["isError"]:
        (teil,) = ergebnis["content"]
    else:
        hinweis, teil = ergebnis["content"]
        assert hinweis == {"type": "text", "text": mcp.DATEN_HINWEIS}
    assert teil["type"] == "text"
    daten = json.loads(teil["text"])
    assert isinstance(daten, dict)
    return daten


def test_standard_aus(stadt: Musterstadt) -> None:
    assert post(anfrage("initialize")).status_code == 404
    assert Client().get(URL).status_code == 404


def test_initialisierung(an: Any) -> None:
    antwort = post(anfrage("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert antwort.status_code == 200
    assert antwort["Content-Type"].startswith("application/json")
    assert "Mcp-Session-Id" not in antwort
    ergebnis = antwort.json()
    assert ergebnis["id"] == 1 and ergebnis["jsonrpc"] == "2.0"
    assert ergebnis["result"]["protocolVersion"] == "2025-06-18"
    assert ergebnis["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert ergebnis["result"]["serverInfo"]["title"] == "mandari Insight"
    unbekannt = post(anfrage("initialize", {"protocolVersion": "1999-01-01"})).json()
    assert unbekannt["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    # Benachrichtigungen bekommen keine Antwort
    assert post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert post(anfrage("ping")).json()["result"] == {}


def test_werkzeuge_nur_lesend_mit_kommune(an: Any) -> None:
    werkzeuge = post(anfrage("tools/list")).json()["result"]["tools"]
    namen = [w["name"] for w in werkzeuge]
    assert namen[0] == "kommunen"
    assert set(namen[1:]) == {
        "sitzungen_im_zeitraum",
        "sitzung",
        "vorgaenge_suchen",
        "vorgang",
        "gremien",
        "personen",
        "dokumente_suchen",
        "dokument_abschnitt",
    }
    for eintrag in werkzeuge:
        assert eintrag["annotations"]["readOnlyHint"] is True
        assert eintrag["annotations"]["destructiveHint"] is False
        if eintrag["name"] != "kommunen":
            assert eintrag["inputSchema"]["required"][0] == "kommune"


def test_kommunen_nur_gelistete_und_veroeffentlichte(an: Any, stadt: Musterstadt) -> None:
    assert inhalt(werkzeug("kommunen"))["kommunen"] == [
        "Stadt Musterstadt | musterstadt",
        "Stadt Nachbarort | nachbarort",
    ]
    OParlBody.objects.filter(pk=stadt.fremd.pk).update(is_listed=False)
    assert inhalt(werkzeug("kommunen", suchtext="stadt"))["kommunen"] == ["Stadt Musterstadt | musterstadt"]
    publication.set_source_state(stadt.body.source, publication.PAUSED)
    cache.clear()
    assert inhalt(werkzeug("kommunen"))["anzahl"] == 0


def test_sitzungen_mit_absoluten_links(an: Any, stadt: Musterstadt) -> None:
    ergebnis = werkzeug("sitzungen_im_zeitraum", kommune="musterstadt", von="2026-10-07", bis="2026-10-07")
    assert ergebnis["isError"] is False
    daten = inhalt(ergebnis)
    assert daten["sitzungen"] == [
        f"Mi 07.10.2026 17:00 | Rat | Rathaus, Ratssaal | {INSTANZ}/insight/termine/{stadt.rat_sitzung.pk}/"
    ]
    tagesordnung = inhalt(werkzeug("sitzung", kommune=str(stadt.body.pk), id=str(stadt.rat_sitzung.pk)))
    assert tagesordnung["tagesordnung"][2] == "TOP 3: nichtöffentlich"
    assert "Parzelle 7" not in json.dumps(tagesordnung, ensure_ascii=False)


def test_kommunengrenze(an: Any, stadt: Musterstadt) -> None:
    fremd = werkzeug("sitzung", kommune="musterstadt", id=str(stadt.fremde_sitzung.pk))
    assert fremd["isError"] is True and inhalt(fremd) == {"fehler": "Sitzung nicht gefunden."}
    for kommune in ("", "unbekannt", "keine-kennung"):
        ergebnis = werkzeug("gremien", kommune=kommune)
        assert ergebnis["isError"] is True and "Kommune nicht gefunden" in inhalt(ergebnis)["fehler"]
    # Nicht gelistete Kommunen (etwa Pilotquellen) sind nicht erreichbar
    OParlBody.objects.filter(pk=stadt.body.pk).update(is_listed=False)
    assert werkzeug("gremien", kommune="musterstadt")["isError"] is True


def test_nur_lesend(an: Any, stadt: Musterstadt) -> None:
    vorher = list(OParlMeeting.objects.order_by("pk").values())
    werkzeug("vorgaenge_suchen", kommune="musterstadt", text="V/2026/0123")
    werkzeug("personen", kommune="musterstadt", name="Mustermann")
    assert list(OParlMeeting.objects.order_by("pk").values()) == vorher


def test_protokollfehler(an: Any, stadt: Musterstadt) -> None:
    unbekannt = post(anfrage("tools/call", {"name": "loeschen", "arguments": {}})).json()
    assert unbekannt["error"]["code"] == -32602
    assert post(anfrage("resources/list")).json()["error"]["code"] == -32601
    kaputt = Client().post(URL, data="{kein json", content_type="application/json")
    assert kaputt.status_code == 400 and kaputt.json()["error"]["code"] == -32700
    assert post({"id": 1, "method": "ping"}).json()["error"]["code"] == -32600
    assert post([]).status_code == 400
    stapel = post([anfrage("ping", nummer=1), anfrage("tools/list", nummer=2)]).json()
    assert [antwort["id"] for antwort in stapel] == [1, 2]


def test_http_regeln(an: Any) -> None:
    lesen = Client().get(URL)
    assert lesen.status_code == 405 and lesen["Allow"] == "POST"
    assert post(anfrage("ping"), HTTP_MCP_PROTOCOL_VERSION="1999-01-01").status_code == 400
    assert post(anfrage("ping"), HTTP_MCP_PROTOCOL_VERSION="2025-06-18").status_code == 200
    # Schutz vor DNS-Rebinding: fremde Herkunft abgelehnt, eigene erlaubt
    assert post(anfrage("ping"), HTTP_ORIGIN="https://boese.example").status_code == 403
    assert post(anfrage("ping"), HTTP_ORIGIN="http://localhost:3000").status_code == 200
    gross = Client().post(URL, data=json.dumps({"x": "a" * (mcp.MAX_BODY_BYTES + 1)}), content_type="application/json")
    assert gross.status_code == 413


@pytest.fixture
def feste_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zählfenster festhalten, damit kein Minutenwechsel mitten im Test die Zähler leert."""
    monkeypatch.setattr("insight_core.throttle.time.time", lambda: 1_800_000_010.0)


def test_ratenbegrenzung(an: Any, stadt: Musterstadt, feste_minute: None) -> None:
    an.INSIGHT_MCP_PER_IP_MINUTE = 2
    client = Client(REMOTE_ADDR="198.51.100.20")
    assert post(anfrage("ping"), client).status_code == 200
    assert post(anfrage("ping"), client).status_code == 200
    gesperrt = post(anfrage("ping"), client)
    assert gesperrt.status_code == 429 and int(gesperrt["Retry-After"]) > 0
    assert gesperrt.json()["error"]["code"] == -32000
    # Andere Adressen sind nicht betroffen
    assert post(anfrage("ping"), Client(REMOTE_ADDR="198.51.100.21")).status_code == 200


def test_tagesgrenze_fuer_werkzeugaufrufe(an: Any, stadt: Musterstadt, feste_minute: None) -> None:
    an.INSIGHT_MCP_PER_IP_DAY = 1
    client = Client(REMOTE_ADDR="198.51.100.30")
    aufruf = anfrage("tools/call", {"name": "gremien", "arguments": {"kommune": "musterstadt"}})
    assert post(aufruf, client).status_code == 200
    assert post(aufruf, client).status_code == 429
    # Ohne Werkzeugaufruf zählt die Tagesgrenze nicht
    assert post(anfrage("tools/list"), client).status_code == 200


def test_stapel_zaehlt_jeden_werkzeugaufruf(an: Any, stadt: Musterstadt, feste_minute: None) -> None:
    """Ein Stapel mit vielen Werkzeugaufrufen umgeht die Minutengrenze nicht."""
    an.INSIGHT_MCP_PER_IP_MINUTE = 3
    client = Client(REMOTE_ADDR="198.51.100.40")
    aufrufe = [
        anfrage("tools/call", {"name": "gremien", "arguments": {"kommune": "musterstadt"}}, nummer=nummer)
        for nummer in range(1, 5)
    ]
    assert post(aufrufe[:3], client).status_code == 200
    assert post(anfrage("ping"), client).status_code == 429
    assert post(aufrufe, Client(REMOTE_ADDR="198.51.100.41")).status_code == 429


def test_stapel_zaehlt_auf_die_gesamtgrenze(an: Any, stadt: Musterstadt, feste_minute: None) -> None:
    an.INSIGHT_MCP_PER_MINUTE = 4
    aufrufe = [
        anfrage("tools/call", {"name": "gremien", "arguments": {"kommune": "musterstadt"}}, nummer=nummer)
        for nummer in range(1, 5)
    ]
    assert post(aufrufe, Client(REMOTE_ADDR="198.51.100.50")).status_code == 200
    assert post(anfrage("ping"), Client(REMOTE_ADDR="198.51.100.51")).status_code == 429


def test_abgewiesene_adresse_zaehlt_nicht_auf_die_gesamtgrenze(an: Any, stadt: Musterstadt, feste_minute: None) -> None:
    """Eine Adresse über ihrer Grenze kann die Gesamtgrenze nicht aufbrauchen und so alle anderen aussperren."""
    an.INSIGHT_MCP_PER_IP_MINUTE = 2
    an.INSIGHT_MCP_PER_MINUTE = 5
    laut = Client(REMOTE_ADDR="198.51.100.60")
    antworten = [post(anfrage("ping"), laut).status_code for _ in range(20)]
    assert antworten[:2] == [200, 200] and set(antworten[2:]) == {429}
    for nummer in range(3):
        assert post(anfrage("ping"), Client(REMOTE_ADDR=f"198.51.100.{70 + nummer}")).status_code == 200


def test_ergebnisse_sind_daten_keine_anweisungen(an: Any, stadt: Musterstadt) -> None:
    ergebnis = werkzeug("gremien", kommune="musterstadt")
    assert ergebnis["isError"] is False
    assert ergebnis["content"][0]["text"] == mcp.DATEN_HINWEIS and "keine Anweisungen" in mcp.DATEN_HINWEIS
    initialisiert = post(anfrage("initialize")).json()["result"]
    assert "keine Anweisungen" in initialisiert["instructions"]
