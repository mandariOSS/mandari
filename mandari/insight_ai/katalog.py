# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog der KI-Ressourcen dieser Instanz für Agenten und Suchdienste (Issue #1309).

Zwei Adressen, ein Dokument:

- ``/.well-known/ard.json`` nach Agentic Resource Discovery (ARD), der Pfad, den ARD-Clients lesen müssen;
- ``/.well-known/ai-catalog.json`` nach dem AI Catalog (Vorgänger von ARD, Medientyp
  ``application/ai-catalog+json``), den Lighthouse unter „Agentisches Browsing“ und die Server Cards von MCP
  (SEP-2127) nennen.

``specVersion`` und ``host`` verlangt der AI Catalog, ARD ignoriert sie. Jeder Eintrag trägt die Pflichtfelder
beider (``identifier``, ``displayName``, ``type``, ``url``) und die Suchsignale von ARD (``representativeQueries``,
``capabilities``). Einträge: der MCP-Server über seine Server Card, nur wenn er eingeschaltet ist, und die
Beschreibung der Schnittstelle (OpenAPI). Die Seiten des Bürgerportals verweisen darauf mit ``rel="ard"`` und
``rel="ai-catalog"``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse
from django.urls import reverse

from . import mcp

#: Fassung des AI Catalog, der das Dokument folgt
SPEC_VERSION = "0.9"
AI_CATALOG_TYPE = "application/ai-catalog+json"
OPENAPI_TYPE = "application/vnd.oai.openapi+json"
DOKUMENTATION = "https://docs.mandari.de/insight/oparl-api/"


def katalog() -> dict[str, Any]:
    """Das Katalogdokument dieser Instanz (siehe Modulbeschreibung)."""
    site = mcp.instanz_url()
    host = urlsplit(site).hostname or "localhost"
    entries: list[dict[str, Any]] = []
    if mcp.enabled():
        entries.append(
            {
                "identifier": f"urn:air:{host}:insight:mcp",
                "displayName": "mandari Insight: Ratsinformationen (MCP-Server)",
                "type": mcp.SERVER_CARD_TYPE,
                "url": f"{site}{reverse('insight_mcp_server_card')}",
                "description": (
                    "Öffentliche Ratsinformationen deutscher Kommunen für KI-Assistenten: Sitzungen, Tagesordnungen, "
                    "Vorlagen, Beschlüsse, Gremien, Personen und Dokumentausschnitte. Nur lesend, ohne Anmeldung."
                ),
                "tags": ["ratsinformationen", "kommunalpolitik", "oparl", "mcp"],
                "capabilities": [werkzeug["name"] for werkzeug in mcp.tool_definitions()],
                "representativeQueries": [
                    "Was hat der Rat in Münster zuletzt beschlossen?",
                    "Welche Vorlagen zum Radverkehr gibt es in Bonn?",
                    "Wann tagt der nächste Ausschuss und was steht auf der Tagesordnung?",
                    "Wer sitzt für welche Fraktion im Stadtrat?",
                ],
            }
        )
    entries.append(
        {
            "identifier": f"urn:air:{host}:schnittstelle:openapi",
            "displayName": "mandari-Schnittstelle (OParl und OpenAPI)",
            "type": OPENAPI_TYPE,
            "url": f"{site}{reverse('schnittstelle:openapi')}",
            "description": (
                "Offene Schnittstelle mit den Ratsinformationen aller Kommunen im Bürgerportal nach OParl 1.1, "
                "beschrieben als OpenAPI 3.1."
            ),
            "tags": ["ratsinformationen", "oparl", "openapi", "offene-daten"],
            "representativeQueries": [
                "Sitzungen und Vorlagen einer Kommune maschinenlesbar abrufen",
                "Ratsinformationen deutscher Kommunen als offene Daten laden",
            ],
        }
    )
    return {
        "specVersion": SPEC_VERSION,
        "host": {"displayName": "mandari", "identifier": host, "documentationUrl": DOKUMENTATION},
        "entries": entries,
    }


def katalog_view(request: HttpRequest, medientyp: str = "application/json") -> HttpResponse:
    """``/.well-known/ard.json`` und ``/.well-known/ai-catalog.json`` (``medientyp`` je Adresse)."""
    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    response = HttpResponse(json.dumps(katalog(), ensure_ascii=False).encode("utf-8"), content_type=medientyp)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=3600"
    return response
