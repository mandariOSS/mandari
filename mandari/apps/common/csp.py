# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Empfang von CSP-Verstoßmeldungen (#172).

Der Browser schickt Verstöße gegen die Content-Security-Policy an ``report-uri``
(Legacy-Format ``application/csp-report``) bzw. ``report-to`` (Reporting API,
``application/reports+json``). Beide Formate landen hier, werden auf die
aussagekräftigen Felder reduziert, als Warnung protokolliert (journald) und als
Prometheus-Zähler je Direktive gezählt. Damit lässt sich die Report-Only-Phase
auswerten, bevor die Policy erzwungen wird.

Schutz: Nur POST, kein CSRF (der Browser sendet ohne Token), Körper höchstens
``MAX_BODY`` Bytes, je Absenderadresse höchstens ``RATE_LIMIT`` Meldungen pro
Minute – darüber hinaus wird still verworfen (204), damit ein fehlkonfigurierter
Client oder ein Angreifer weder Protokoll noch Metriken fluten kann.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from prometheus_client import Counter

from apps.accounts.two_factor_policy import client_ip

logger = logging.getLogger("mandari.csp")

MAX_BODY = 16 * 1024
RATE_LIMIT = 60  # Meldungen je Adresse und Minute
FIELDS = ("effective-directive", "blocked-uri", "document-uri", "source-file", "line-number", "disposition")
# Browser-Erweiterungen schleusen eigene Skripte und Schriften ein; ihre Meldungen sagen nichts über mandari aus
EXTENSION_SCHEMES = ("moz-extension:", "chrome-extension:", "safari-extension:", "safari-web-extension:")

VIOLATIONS = Counter("mandari_csp_violations_total", "Gemeldete CSP-Verstöße je Direktive", ["directive"])


#: Direktiven, die als eigene Reihe der Metrik zählen. Die Meldungen kommen ohne Anmeldung von außen: Jeder andere
#: Wert zählt als „unbekannt“, damit fremde Eingaben weder beliebig viele Reihen anlegen noch als Zeichenfolge in
#: die Ausgabe der Metriken gelangen.
KNOWN_DIRECTIVES = frozenset(
    {
        "base-uri",
        "child-src",
        "connect-src",
        "default-src",
        "font-src",
        "form-action",
        "frame-ancestors",
        "frame-src",
        "img-src",
        "manifest-src",
        "media-src",
        "object-src",
        "report-to",
        "report-uri",
        "require-trusted-types-for",
        "sandbox",
        "script-src",
        "script-src-attr",
        "script-src-elem",
        "style-src",
        "style-src-attr",
        "style-src-elem",
        "trusted-types",
        "upgrade-insecure-requests",
        "worker-src",
    }
)
UNKNOWN_DIRECTIVE = "unbekannt"


def directive_label(raw: str) -> str:
    """Direktive einer Meldung als Label der Metrik: eine bekannte Direktive oder ``unbekannt``."""
    first = raw.split()[0].lower() if raw.split() else ""
    return first if first in KNOWN_DIRECTIVES else UNKNOWN_DIRECTIVE


def _normalize(report: dict[str, Any]) -> dict[str, str]:
    """Legacy- und Reporting-API-Felder auf einheitliche Namen bringen."""
    aliases = {
        "effectiveDirective": "effective-directive",
        "blockedURL": "blocked-uri",
        "documentURL": "document-uri",
        "sourceFile": "source-file",
        "lineNumber": "line-number",
    }
    merged = {aliases.get(k, k): v for k, v in report.items()}
    # Einzelne Surrogate (in JSON als Escape möglich) lassen sich nicht ausgeben: durch „?“ ersetzen
    return {feld: str(merged.get(feld, ""))[:300].encode("utf-8", "replace").decode("utf-8") for feld in FIELDS}


def parse_reports(raw: bytes, content_type: str) -> list[dict[str, str]]:
    """Meldungen aus dem Anfragekörper lesen; ungültige Körper ergeben eine leere Liste."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    reports: list[dict[str, Any]] = []
    if "reports+json" in content_type and isinstance(data, list):
        for eintrag in data:
            body = eintrag.get("body") if isinstance(eintrag, dict) else None
            if isinstance(body, dict) and eintrag.get("type", "csp-violation") == "csp-violation":
                reports.append(body)
    elif isinstance(data, dict):
        body = data.get("csp-report", data)
        if isinstance(body, dict):
            reports.append(body)
    return [_normalize(r) for r in reports[:20]]


def from_extension(report: dict[str, str]) -> bool:
    """Stammt der Verstoß aus einer Browser-Erweiterung statt aus mandari?"""
    return any(report[feld].startswith(EXTENSION_SCHEMES) for feld in ("source-file", "blocked-uri"))


def _rate_limited(request: HttpRequest) -> bool:
    key = f"csp-report:{client_ip(request)}"
    cache.add(key, 0, 60)
    try:
        return int(cache.incr(key)) > RATE_LIMIT
    except ValueError:
        return False


@csrf_exempt
@require_POST
def csp_report(request: HttpRequest) -> HttpResponse:
    """Verstoßmeldung entgegennehmen: immer 204, Auswertung über Protokoll und Metriken."""
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY or len(request.body) > MAX_BODY:
        return HttpResponse(status=204)
    if _rate_limited(request):
        return HttpResponse(status=204)
    for report in parse_reports(request.body, request.content_type or ""):
        if from_extension(report):
            continue
        directive = directive_label(report["effective-directive"])
        VIOLATIONS.labels(directive=directive).inc()
        logger.warning(
            "CSP-Verstoß %s: blockiert=%s seite=%s quelle=%s:%s (%s)",
            directive,
            report["blocked-uri"],
            report["document-uri"],
            report["source-file"],
            report["line-number"],
            report["disposition"] or "report",
        )
    return HttpResponse(status=204)
