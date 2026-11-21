# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server Card des MCP-Servers, Katalog unter /.well-known/ und WebMCP an den Formularen (Issue #1309)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from insight_ai import mcp
from insight_core.models import OParlBody
from insight_core.templatetags.webmcp import WERKZEUGE

from .musterstadt import FakeSuche, Musterstadt, baue_musterstadt

pytestmark = pytest.mark.django_db

INSTANZ = "https://mandari.example"
#: Name der Server Card laut Schema (SEP-2127): umgekehrte DNS-Schreibweise mit genau einem Schrägstrich
NAME_MUSTER = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")


@pytest.fixture
def an(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.INSIGHT_MCP_ENABLED = True
    settings.SITE_URL = INSTANZ
    cache.clear()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: FakeSuche())
    return settings


@pytest.fixture
def stadt() -> Musterstadt:
    stadt = baue_musterstadt()
    OParlBody.objects.filter(pk=stadt.body.pk).update(slug="musterstadt")
    return stadt


class TestServerCard:
    def test_ausgeschaltet_nicht_gefunden(self, settings: Any) -> None:
        settings.INSIGHT_MCP_ENABLED = False
        assert Client().get("/insight/mcp/server-card").status_code == 404

    def test_karte(self, an: Any) -> None:
        antwort = Client().get("/insight/mcp/server-card")
        assert antwort.status_code == 200
        assert antwort["Content-Type"] == "application/mcp-server-card+json"
        assert antwort["Access-Control-Allow-Origin"] == "*"
        karte = json.loads(antwort.content)
        assert karte["$schema"] == "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
        assert karte["name"] == "example.mandari/insight" and NAME_MUSTER.match(karte["name"])
        assert 1 <= len(karte["description"]) <= 100
        assert len(karte["title"]) <= 100
        assert karte["websiteUrl"] == f"{INSTANZ}/insight/"
        assert karte["remotes"] == [
            {
                "type": "streamable-http",
                "url": f"{INSTANZ}/insight/mcp",
                "supportedProtocolVersions": list(mcp.PROTOCOL_VERSIONS),
            }
        ]

    def test_nur_lesen(self, an: Any) -> None:
        antwort = Client().post("/insight/mcp/server-card", data="{}", content_type="application/json")
        assert antwort.status_code == 405 and antwort["Allow"] == "GET, HEAD"

    def test_server_info_wie_karte(self, an: Any) -> None:
        nachricht = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        antwort = Client().post("/insight/mcp", data=json.dumps(nachricht), content_type="application/json")
        info = antwort.json()["result"]["serverInfo"]
        karte = mcp.server_card()
        assert (info["name"], info["version"], info["title"]) == (karte["name"], karte["version"], karte["title"])

    def test_name_ohne_unerlaubte_zeichen(self, settings: Any) -> None:
        settings.SITE_URL = "http://[::1]:8000"
        assert NAME_MUSTER.match(mcp.server_name())


class TestKatalog:
    @pytest.mark.parametrize(
        ("adresse", "medientyp"),
        [
            ("/.well-known/ard.json", "application/json"),
            ("/.well-known/ai-catalog.json", "application/ai-catalog+json"),
        ],
    )
    def test_beide_adressen(self, an: Any, adresse: str, medientyp: str) -> None:
        antwort = Client().get(adresse)
        assert antwort.status_code == 200
        assert antwort["Content-Type"] == medientyp
        assert antwort["Access-Control-Allow-Origin"] == "*"
        katalog = json.loads(antwort.content)
        assert katalog["specVersion"] == "0.9" and katalog["host"]["displayName"]
        for eintrag in katalog["entries"]:
            # Pflichtfelder von ARD und AI Catalog, genau eins von url und data
            assert {"identifier", "displayName", "type", "url"} <= set(eintrag) and "data" not in eintrag
            assert eintrag["identifier"].startswith("urn:air:mandari.example:")
            assert 2 <= len(eintrag["representativeQueries"]) <= 5
        mcp_eintrag = next(e for e in katalog["entries"] if e["type"] == "application/mcp-server-card+json")
        assert mcp_eintrag["url"] == f"{INSTANZ}/insight/mcp/server-card"
        assert "kommunen" in mcp_eintrag["capabilities"]
        openapi = next(e for e in katalog["entries"] if e["type"] == "application/vnd.oai.openapi+json")
        assert openapi["url"] == f"{INSTANZ}/api/openapi.json"

    def test_ohne_mcp_nur_schnittstelle(self, an: Any) -> None:
        an.INSIGHT_MCP_ENABLED = False
        katalog = Client().get("/.well-known/ard.json").json()
        assert [e["type"] for e in katalog["entries"]] == ["application/vnd.oai.openapi+json"]

    def test_nur_lesen(self, an: Any) -> None:
        assert Client().post("/.well-known/ai-catalog.json").status_code == 405


class TestWebmcp:
    """Jedes Werkzeug höchstens einmal je Seite, Katalog-Links in jeder Seite, Token nur mit Einstellung."""

    @staticmethod
    def _seite(client: Client, name: str, **kwargs: Any) -> str:
        antwort = client.get(reverse(f"insight_core:insight:{name}"), **kwargs)
        assert antwort.status_code == 200
        return antwort.content.decode()

    def test_werkzeuge_je_seite(self, an: Any, stadt: Musterstadt) -> None:
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[stadt.body.id]))
        uebersicht = self._seite(client, "portal_home")
        vorgaenge = self._seite(client, "paper_list")
        suche = self._seite(client, "search", data={"q": "Radweg"})
        for seite in (uebersicht, vorgaenge, suche):
            assert seite.count('toolname="ratsinfo_suchen"') == 1
            assert 'toolparamdescription="Suchbegriff' in seite
            # Der Kommunenwechsel im Dialog bleibt ohne Werkzeug
            assert 'toolname="kommune_finden"' not in seite
            assert 'rel="ard"' in seite and 'href="/.well-known/ard.json"' in seite
            assert 'rel="ai-catalog"' in seite
            assert 'http-equiv="origin-trial"' not in seite
        # Auswahlseite ohne Kommune: Kommune finden als Werkzeug
        client.get(reverse("insight_core:insight:clear_body"))
        auswahl = self._seite(client, "portal_home")
        assert auswahl.count('toolname="kommune_finden"') == 1

    def test_origin_trial_token(self, an: Any, stadt: Musterstadt) -> None:
        an.WEBMCP_ORIGIN_TRIAL_TOKEN = "TOKEN123"
        client = Client()
        client.get(reverse("insight_core:insight:set_body", args=[stadt.body.id]))
        assert '<meta http-equiv="origin-trial" content="TOKEN123">' in self._seite(client, "paper_list")

    def test_beschreibungen_ohne_feldaufzaehlung(self) -> None:
        # Chrome: tooldescription sagt, was das Werkzeug tut und wann; die Felder nennt der Browser selbst
        for werkzeug in WERKZEUGE.values():
            assert len(str(werkzeug["beschreibung"])) <= 300
            assert "q" in werkzeug["felder"]  # type: ignore[operator]
