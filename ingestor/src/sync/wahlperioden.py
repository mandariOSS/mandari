# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wahlperioden aus der Liste ``legislativeTermList`` einer Kommune (Issue #1453).

Viele Quellen nennen die Liste, betten aber keine Wahlperiode in den Body ein; ohne die Liste blieben diese Kommunen
ohne Wahlperiode. Gefunden wurden leere Listen (auch als JSON-Array ohne Hülle), HTTP 401 und HTTP 503. Die Liste ist
klein und ändert sich selten, und sie darf den Abgleich der Quelle nicht stören:

- **Leise:** ``OParlClient.fetch_optional``: kein Befund am Host, kein Schutzschalter, keine Serie von Serverfehlern;
  ein Fehler ist keine Lücke im Abgleich (``incomplete``).
- **Höchstens einmal am Tag** je Liste, im Vollabgleich jedes Mal. Zeitpunkt und Ergebnis jeder Prüfung stehen in
  ``sync_config["legislative_term_lists"]``.
- **Löschmarken** der Liste (``deleted: true``) markieren schon gespeicherte Wahlperioden; eine Wahlperiode, die nur
  fehlt, bleibt (die Liste kann gekürzt oder gestört sein).
- Eine Wahlperiode, die eine andere Kommune derselben Quelle schon unter derselben Kennung gespeichert hat, wird mit
  dieser Kommune verknüpft (``upsert_legislative_term``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.client.oparl_client import FetchResult, OParlClient

#: Abstand zwischen zwei Prüfungen derselben Liste im inkrementellen Lauf
PRUEFABSTAND = timedelta(days=1)
#: Höchstzahl gelesener Seiten; Wahlperioden passen in aller Regel auf eine Seite
MAX_SEITEN = 10


@dataclass
class WahlperiodenListen:
    """Letzte Prüfung je Wahlperiodenliste einer Quelle (Adresse -> Zeitpunkt), gelesen beim Start ihres Abgleichs."""

    source_url: str
    geprueft: dict[str, datetime | None] = field(default_factory=dict)

    def faellig(self, list_url: str, *, full: bool, now: datetime | None = None) -> bool:
        """Im Vollabgleich immer, sonst wenn die letzte Prüfung einen Tag zurückliegt oder unbekannt ist."""
        if full:
            return True
        zuletzt = self.geprueft.get(list_url)
        if zuletzt is None:
            return True
        alter = (now or datetime.now(UTC)) - zuletzt
        return not timedelta(0) <= alter < PRUEFABSTAND


@dataclass
class Gelesen:
    """Einträge der Liste und das Ergebnis der Prüfung für den Vermerk."""

    eintraege: list[dict[str, Any]]
    ergebnis: str


def _fehler(result: FetchResult) -> str:
    if result.status_code == 404:
        return "HTTP 404, Liste fehlt"
    if result.status_code:
        return f"HTTP {result.status_code}"
    if result.timed_out:
        return "Zeitüberschreitung"
    return result.error or "nicht lesbar"


def _seite(data: Any) -> tuple[list[Any], str | None] | None:
    """Objekte und Folgeseite einer Antwort: OParl-Liste (``data``, ``links.next``) oder JSON-Array; sonst ``None``."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        links = data.get("links")
        weiter = links.get("next") if isinstance(links, dict) else None
        return data["data"], weiter if isinstance(weiter, str) and weiter else None
    return None


async def lies_liste(client: OParlClient, list_url: str) -> Gelesen:
    """
    Liste lesen, höchstens ``MAX_SEITEN`` Seiten. Scheitert eine Folgeseite, gilt das Gelesene (die Liste ergänzt nur,
    sie löscht nichts durch Fehlen); scheitert die erste, ist die Liste leer und das Ergebnis nennt den Grund.
    """
    eintraege: list[dict[str, Any]] = []
    gelesen: set[str] = set()
    adresse: str | None = list_url
    while adresse is not None and len(gelesen) < MAX_SEITEN and adresse not in gelesen:
        gelesen.add(adresse)
        result = await client.fetch_optional(adresse)
        seite = _seite(result.data) if result.error is None else None
        if seite is None:
            grund = _fehler(result) if result.error is not None else "keine OParl-Liste"
            if not eintraege:
                return Gelesen([], grund)
            return Gelesen(eintraege, f"{len(eintraege)} gelesen, dann {grund}")
        objekte, adresse = seite
        eintraege.extend(o for o in objekte if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"])
    return Gelesen(eintraege, f"{len(eintraege)} gelesen" if eintraege else "leer")
