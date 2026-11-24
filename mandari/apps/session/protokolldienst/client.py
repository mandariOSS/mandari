# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufrufe der Sitzungs- und Ereignis-Schnittstelle des Protokolldienstes (``/api/v1/`` des Dienstes).

- ``sitzung_finden``: Die Sitzung beim Dienst über ihre OParl-Kennung (``GET /api/v1/meetings`` im Zeitraum um den
  Beginn), dazu ihre TOPs (``GET /api/v1/meetings/{id}``) für die Zuordnung über die TOP-Nummer.
- ``ereignisse_senden``: ``POST /api/v1/meetings/{id}/events`` mit Quelle ``session`` und ``external_id`` je Meldung;
  gleiche Kennungen speichert der Dienst nur einmal, Wiederholungen sind also unschädlich.

Der Schlüssel braucht beim Dienst die Rechte ``meetings:read`` und ``events:write``.

**Ziele:** Hosts aus ``PROTOKOLLDIENST_ERLAUBTE_HOSTS`` gibt der Betrieb frei; sonst verbindet der Client nur mit
öffentlichen Adressen, geprüft beim Verbindungsaufbau (``insight_core.services.safe_fetch``, streng). Keine
Weiterleitungen.

**Fehler** werden zu ``ProtokolldienstError`` mit festem Code, nie mit Antwortinhalten: ``anmeldung`` (401, 403) und
``abgelehnt`` (400, 413, 422) sind dauerhaft, ``sitzung_unbekannt`` (die Sitzung ist beim Dienst noch nicht
übernommen), ``ueberlastet`` (429), ``dienstfehler`` (5xx), ``zeitgrenze`` und ``verbindung`` werden wiederholt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final
from urllib.parse import quote

import httpx

from apps.session.models import SessionMeeting, SessionProtokolldienst

from . import einstellungen

TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
USER_AGENT: Final = "mandari-session (Protokolldienst)"
QUELLE: Final = "session"
#: So weit um den geplanten Beginn sucht ``sitzung_finden`` (verschobene Sitzungen)
SUCHFENSTER: Final = timedelta(days=3)
#: Größte Antwort, die gelesen wird
MAX_ANTWORT: Final = 2 * 1024 * 1024


class ProtokolldienstError(Exception):
    """Aufruf gescheitert; ``code`` ist ein fester Code, ``dauerhaft`` heißt: Wiederholen hilft nicht."""

    def __init__(self, code: str, *, dauerhaft: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.dauerhaft = dauerhaft


def neuer_client(konfiguration: SessionProtokolldienst) -> httpx.Client:
    """Client für die Adresse der Anbindung; Aufrufer schließt ihn (``with``). Tests ersetzen diese Funktion."""
    kopf = {
        "Authorization": f"Bearer {einstellungen.api_schluessel(konfiguration)}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if einstellungen.freigegebener_host(konfiguration.base_url):
        return httpx.Client(timeout=TIMEOUT, headers=kopf, follow_redirects=False)
    from insight_core.services import safe_fetch

    return safe_fetch.guarded_client(strict=True, timeout=TIMEOUT, headers=kopf, follow_redirects=False)


def _adresse(konfiguration: SessionProtokolldienst, pfad: str) -> str:
    return f"{konfiguration.base_url.rstrip('/')}/api/v1/{pfad}"


def _fehler_aus_status(status: int) -> ProtokolldienstError:
    if status in (401, 403):
        return ProtokolldienstError("anmeldung", dauerhaft=True)
    if status == 404:
        return ProtokolldienstError("sitzung_unbekannt")
    if status == 429:
        return ProtokolldienstError("ueberlastet")
    if status >= 500:
        return ProtokolldienstError("dienstfehler")
    return ProtokolldienstError("abgelehnt", dauerhaft=True)


def _aufruf(http: httpx.Client, methode: str, adresse: str, **kwargs: Any) -> Any:
    try:
        antwort = http.request(methode, adresse, **kwargs)
    except httpx.TimeoutException:
        raise ProtokolldienstError("zeitgrenze") from None
    except httpx.HTTPError:
        # Auch ein gesperrtes Ziel (BlockedDestinationError): ohne Inhalt, später erneut versucht
        raise ProtokolldienstError("verbindung") from None
    if antwort.status_code >= 300:
        raise _fehler_aus_status(antwort.status_code)
    if len(antwort.content) > MAX_ANTWORT:
        raise ProtokolldienstError("abgelehnt", dauerhaft=True)
    try:
        return antwort.json()
    except ValueError:
        raise ProtokolldienstError("dienstfehler") from None


def sitzung_finden(
    http: httpx.Client, konfiguration: SessionProtokolldienst, meeting: SessionMeeting
) -> tuple[str, dict[str, str]]:
    """
    Kennung der Sitzung beim Dienst und ihre TOPs (TOP-Nummer → Kennung beim Dienst).

    Wirft ``ProtokolldienstError("sitzung_unbekannt")``, solange der Dienst die Sitzung nicht übernommen hat.
    """
    kennung = einstellungen.sitzung_kennung(meeting)
    beginn = meeting.start
    liste = _aufruf(
        http,
        "GET",
        _adresse(konfiguration, "meetings"),
        params={"start_from": (beginn - SUCHFENSTER).isoformat(), "start_to": (beginn + SUCHFENSTER).isoformat()},
    )
    treffer = next(
        (
            eintrag
            for eintrag in (liste if isinstance(liste, list) else [])
            if isinstance(eintrag, dict) and einstellungen.gleiche_kennung(str(eintrag.get("oparl_id") or ""), kennung)
        ),
        None,
    )
    if treffer is None or not treffer.get("id"):
        raise ProtokolldienstError("sitzung_unbekannt")
    dienst_id = str(treffer["id"])
    sitzung = _aufruf(http, "GET", _adresse(konfiguration, f"meetings/{quote(dienst_id, safe='')}"))
    tops: dict[str, str] = {}
    for top in sitzung.get("agenda_items", []) if isinstance(sitzung, dict) else []:
        if isinstance(top, dict) and top.get("id") and str(top.get("number") or "").strip():
            tops.setdefault(str(top["number"]).strip(), str(top["id"]))
    return dienst_id, tops


def ereignisse_senden(
    http: httpx.Client, konfiguration: SessionProtokolldienst, dienst_id: str, ereignisse: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ereignisse gebündelt senden; liefert je Ereignis das Ergebnis des Dienstes (``index``, ``created``, ``error``)."""
    antwort = _aufruf(
        http,
        "POST",
        _adresse(konfiguration, f"meetings/{quote(dienst_id, safe='')}/events"),
        json={"events": ereignisse},
    )
    if not isinstance(antwort, list):
        raise ProtokolldienstError("dienstfehler")
    return [eintrag for eintrag in antwort if isinstance(eintrag, dict)]


def verbindung_pruefen(konfiguration: SessionProtokolldienst) -> None:
    """Erreichbarkeit und Anmeldung prüfen (``GET /api/v1/meetings`` ohne Zeitraum); wirft ``ProtokolldienstError``."""
    with neuer_client(konfiguration) as http:
        _aufruf(http, "GET", _adresse(konfiguration, "meetings"), params={"start_from": "2100-01-01T00:00:00+00:00"})
