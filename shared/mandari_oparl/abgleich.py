# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wann ein Abgleich auf „in der Quelle gelöscht“ schließen darf – gemeinsam für Ingestor und Django (Issue #556).

Nicht erreichbar ist nicht gelöscht. Als gelöscht gilt ein Objekt nur,

- wenn die Quelle es ausdrücklich meldet (OParl ``deleted: true``, im Ingestor),
- wenn die Quelle seine Adresse mit :data:`GONE_STATUS` beantwortet (Dokumente; der Löschabgleich in
  Django bestätigt das per GET, bevor er sperrt, und prüft vor dem Löschen erneut), oder
- wenn es nach einem **vollständig erfolgreichen** Vollabgleich fehlt (Scraper-Quellen ohne Löschsignal;
  erst nach mehreren solchen Abgleichen in Folge).

Keinen Schluss auf „gelöscht“ erlauben: Netzfehler und Zeitüberschreitungen, andere 4xx (401, 403, 451 …),
5xx, :data:`THROTTLE_STATUS`, eine Sperr- oder Bot-Schutz-Seite (HTML statt Datei, Prüfseite statt
RIS-Seite), eine abgebrochene Paginierung und Teilantworten. Ein Lauf mit einem dieser Befunde ist für
die betroffenen Objekte nicht vollständig.

**Bremse:** Fehlen in einem Lauf mehr Objekte einer Quelle als die Grenze, schließt der Lauf auf keines
davon (:func:`brake_engaged`). Ein Bruch in der Quelle oder im Parser ist dann wahrscheinlicher als eine
Massenlöschung; der Betrieb prüft. Datei-Löschabgleich und Scraper-Abgleich nutzen dieselbe Regel.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: Antworten, mit denen die Quelle sagt: Unter dieser Adresse gibt es das Objekt nicht (mehr)
GONE_STATUS: Final = frozenset({404, 410})
#: Antworten, mit denen ein Host bremst: nichts schließen, in diesem Lauf nicht weiter anfragen
THROTTLE_STATUS: Final = frozenset({429, 503})

#: Merkmale von Bot-Gates und WAF-Seiten (klein geschrieben), je Art
GATE_MARKERS: Final[Mapping[str, tuple[str, ...]]] = {
    "browser_verification": (
        "just a moment",
        "verifying your browser",
        "checking your browser",
        "browser-verifikation",
        "browser verification",
        "cf-chl",
        "challenge-platform",
        "cf_chl_opt",
    ),
    "proof_of_work": ("altcha", "proof-of-work", "proof of work", "pow-challenge", "anubis"),
    "waf_forbidden": ("access denied", "request blocked", "zugriff verweigert", "web application firewall"),
}


def is_gone(status: int | None) -> bool:
    """Sagt die Antwort, dass es das Objekt unter dieser Adresse nicht (mehr) gibt?"""
    return status in GONE_STATUS


def looks_like_html(data: bytes) -> bool:
    """Beginnt der Inhalt wie eine HTML-Seite (Hinweis-, Fehler- oder Sperrseite statt einer Datei)?"""
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:200]


def detect_gate(status: int | None, html: str, headers: Mapping[str, str] | None = None) -> str | None:
    """
    Bot-Gate oder WAF-Seite erkennen: Art aus :data:`GATE_MARKERS` oder ``None`` (keine Sperre erkennbar).

    Nur zum Einordnen einer Seite, die schon aus anderem Grund nicht passt (falscher Status, fremder
    Aufbau): Die Merkmale können auch im Text einer echten Seite stehen.
    """
    text = (html or "").lower()
    kopf = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    if kopf.get("cf-mitigated") == "challenge":
        return "browser_verification"
    for art, marker in GATE_MARKERS.items():
        if any(m in text for m in marker):
            return art
    if status in (403, 429, 503) and "cloudflare" in kopf.get("server", ""):
        return "waf_forbidden"
    return None


def brake_engaged(missing: int, limit: int) -> bool:
    """Bremse: Fehlen in einem Lauf mehr als ``limit`` Objekte einer Quelle, wird keines als gelöscht gewertet."""
    return missing > max(0, limit)
