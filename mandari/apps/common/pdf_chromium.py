# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PDF aus HTML über headless Chromium (Gotenberg, Issue #1584).

Für Dokumente, deren Seiten die Satz-Kette setzt (frontend/satz/): Dieselbe Kette läuft im Editor und hier im
Chromium; deshalb brechen Zeilen und Seiten im PDF genau wie in der Seitenansicht des Editors um.

Der Dienst bekommt alle Dateien mit (HTML, Skripte, Schriften, Stile) und lädt nichts nach; er läuft in einem eigenen
Container ohne Zugriff nach außen (docker-compose.yml: ``--chromium-allow-list`` nur für die hochgeladenen Dateien).
Ohne ``GOTENBERG_URL`` oder bei einem Fehler meldet :func:`html_zu_pdf` ``ChromiumNichtVerfuegbarError``; die Aufrufer
nehmen dann den bisherigen Weg (apps/common/pdf.py).
"""

from __future__ import annotations

import logging
import mimetypes
import time

import httpx
from django.conf import settings

from apps.common.metrics import PDF_DOCUMENTS, PDF_DURATION

logger = logging.getLogger(__name__)

#: DIN A4 in Zoll (Gotenberg rechnet in Zoll)
A4_BREITE_ZOLL = 210 / 25.4
A4_HOEHE_ZOLL = 297 / 25.4
#: Größte Antwort (PDF) in Bytes
MAX_PDF_BYTES = 100 * 1024 * 1024


class ChromiumNichtVerfuegbarError(RuntimeError):
    """Kein Dienst eingerichtet oder die Umwandlung ist gescheitert; der Aufrufer nimmt den bisherigen Weg."""


def verfuegbar() -> bool:
    return bool(getattr(settings, "GOTENBERG_URL", ""))


def _art(name: str) -> str:
    if name.endswith(".woff2"):
        return "font/woff2"
    if name.endswith(".js"):
        return "text/javascript"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def html_zu_pdf(dateien: dict[str, bytes], *, warten_auf: str = "window.satzFertig === true") -> bytes:
    """
    ``dateien`` enthält ``index.html`` und alles, was sie lädt, unter flachen Namen (ohne Verzeichnisse). Die Seite
    meldet über ``warten_auf``, dass sie fertig gesetzt ist; eine Ausnahme in ihrer Konsole bricht ab.
    """
    if not verfuegbar():
        raise ChromiumNichtVerfuegbarError("GOTENBERG_URL ist nicht gesetzt")
    if "index.html" not in dateien:
        raise ValueError("index.html fehlt")
    start = time.perf_counter()
    felder = {
        "paperWidth": f"{A4_BREITE_ZOLL:.4f}",
        "paperHeight": f"{A4_HOEHE_ZOLL:.4f}",
        "marginTop": "0",
        "marginBottom": "0",
        "marginLeft": "0",
        "marginRight": "0",
        "preferCssPageSize": "true",
        "printBackground": "true",
        "waitForExpression": warten_auf,
        "failOnConsoleExceptions": "true",
        "skipNetworkIdleEvent": "true",
    }
    teile = [("files", (name, inhalt, _art(name))) for name, inhalt in dateien.items()]
    url = f"{settings.GOTENBERG_URL}/forms/chromium/convert/html"
    try:
        with httpx.Client(timeout=settings.GOTENBERG_TIMEOUT) as client:
            antwort = client.post(url, data=felder, files=teile)
    except httpx.HTTPError as fehler:
        PDF_DOCUMENTS.labels(result="failed").inc()
        logger.warning("PDF über Chromium nicht möglich: %s", type(fehler).__name__)
        raise ChromiumNichtVerfuegbarError(type(fehler).__name__) from fehler
    pdf = antwort.content
    if antwort.status_code != 200 or not pdf.startswith(b"%PDF") or len(pdf) > MAX_PDF_BYTES:
        PDF_DOCUMENTS.labels(result="failed").inc()
        # Nur der Status: Der Text der Antwort kann Inhalt des Dokuments wiedergeben
        logger.warning("PDF über Chromium abgelehnt: HTTP %s", antwort.status_code)
        raise ChromiumNichtVerfuegbarError(f"HTTP {antwort.status_code}")
    PDF_DURATION.observe(time.perf_counter() - start)
    PDF_DOCUMENTS.labels(result="ok").inc()
    return pdf
