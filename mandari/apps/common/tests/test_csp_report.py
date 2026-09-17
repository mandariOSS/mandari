# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSP-Report-Endpunkt (#172): beide Meldeformate, Protokoll und Zähler, Limits."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.common import csp

LEGACY = {
    "csp-report": {
        "document-uri": "https://mandari.example/work/",
        "effective-directive": "script-src-elem",
        "blocked-uri": "inline",
        "source-file": "https://mandari.example/work/",
        "line-number": 42,
        "disposition": "report",
    }
}
REPORTING_API = [
    {
        "type": "csp-violation",
        "body": {
            "documentURL": "https://mandari.example/session/",
            "effectiveDirective": "style-src-attr",
            "blockedURL": "inline",
            "sourceFile": "https://mandari.example/session/",
            "lineNumber": 7,
            "disposition": "report",
        },
    }
]


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def _zaehler(directive: str) -> float:
    return float(csp.VIOLATIONS.labels(directive=directive)._value.get())


def test_parse_beide_formate() -> None:
    legacy = csp.parse_reports(json.dumps(LEGACY).encode(), "application/csp-report")
    modern = csp.parse_reports(json.dumps(REPORTING_API).encode(), "application/reports+json")
    assert legacy[0]["effective-directive"] == "script-src-elem" and legacy[0]["line-number"] == "42"
    assert modern[0]["effective-directive"] == "style-src-attr" and modern[0]["document-uri"].endswith("/session/")
    assert csp.parse_reports(b"kein json", "application/csp-report") == []
    assert csp.parse_reports(b"[1, 2]", "application/reports+json") == []


def test_meldung_wird_protokolliert_und_gezaehlt(client: Client, caplog: pytest.LogCaptureFixture) -> None:
    vorher = _zaehler("script-src-elem")
    with caplog.at_level(logging.WARNING, logger="mandari.csp"):
        response = client.post("/csp-report/", data=json.dumps(LEGACY), content_type="application/csp-report")
    assert response.status_code == 204
    assert _zaehler("script-src-elem") == vorher + 1
    assert any("CSP-Verstoß script-src-elem" in r.getMessage() and "inline" in r.getMessage() for r in caplog.records)


def test_nur_post_und_ohne_csrf(client: Client) -> None:
    assert client.get("/csp-report/").status_code == 405
    strenger = Client(enforce_csrf_checks=True)
    assert (
        strenger.post("/csp-report/", data=json.dumps(LEGACY), content_type="application/csp-report").status_code == 204
    )


def test_zu_grosse_koerper_und_ratenlimit_werden_still_verworfen(client: Client) -> None:
    vorher = _zaehler("style-src-attr")
    riesig = json.dumps({"csp-report": {"effective-directive": "style-src-attr", "blocked-uri": "x" * csp.MAX_BODY}})
    assert client.post("/csp-report/", data=riesig, content_type="application/csp-report").status_code == 204
    assert _zaehler("style-src-attr") == vorher

    body: Any = json.dumps(REPORTING_API)
    for _ in range(csp.RATE_LIMIT + 5):
        assert client.post("/csp-report/", data=body, content_type="application/reports+json").status_code == 204
    assert _zaehler("style-src-attr") == vorher + csp.RATE_LIMIT
