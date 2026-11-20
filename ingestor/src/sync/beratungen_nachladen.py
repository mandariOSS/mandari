# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen anstehender Sitzungen einzeln nachladen (Issue #1523).

Ein Tagesordnungspunkt nennt seine Beratung (``AgendaItem.consultation``); fehlt sie im Bestand, findet Work am Punkt
keine Vorlage. Das passiert, obwohl die Schnittstelle die Beratung einzeln ausliefert:

- more! rubin (``*.gremien.info``, OParl 1.0) legt bei jeder Änderung einer Vorlage alle ihre Beratungen mit neuer
  Kennung an. Der Punkt nennt die neue, der Änderungsabgleich erreicht die geänderte Vorlage aber nicht (Filter
  ignoriert, Stopp nach Seiten ohne Änderung); erst der nächste Vollabgleich brächte sie.
- SD.NET RIM gibt Vorlagen und Beratungen mit älterem ``modified`` frei; der Änderungsabgleich sieht sie nie.

Nach den Listen einer Kommune ruft der Abgleich deshalb Beratungen, die Punkte der Sitzungen von ``ZURUECK`` bis
``VORAUS`` nennen und die es nicht gibt, einzeln ab und speichert sie wie im Abgleich; ist ihre Vorlage unbekannt, auch
diese (sie bringt Dateien und alle eingebetteten Beratungen mit). Anstehende Sitzungen zuerst.

Abrufe: höchstens ``sync_config["beratungen_nachladen"]`` je Lauf einer Kommune (Standard ``STANDARD_KONTINGENT``,
``0`` schaltet aus), Takt, Kennung, robots.txt und Abruffenster wie beim übrigen Abruf (``OParlClient.fetch``). Nur
Adressen auf dem Server der Kommune, keine synthetischen Kennungen. Eine Beratung, die sich nicht laden oder
speichern ließ, kommt frühestens nach ``ERNEUT_NACH`` wieder an die Reihe
(``sync_config["beratungen_nachladen_versuche"]``). Eine Sperre oder Störung des Servers beendet das Nachladen für
diesen Lauf. Fehler halten den Abgleich nicht auf.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse

from src.client.oparl_compat import is_oparl_error
from src.redaction import MaskingConsole

console = MaskingConsole()

#: Schlüssel in ``sync_config``: Abrufe je Lauf einer Kommune (``0``: aus)
KONTINGENT_SCHLUESSEL = "beratungen_nachladen"
STANDARD_KONTINGENT = 60
#: Zeitraum der Sitzungen, deren Punkte nachgeladen werden
ZURUECK = timedelta(days=14)
VORAUS = timedelta(days=60)
#: Eine Beratung, die sich nicht laden ließ, erst danach wieder versuchen
ERNEUT_NACH = timedelta(days=1)


class Abruf(Protocol):
    """Was das Nachladen von einem Abruf braucht (``FetchResult``)."""

    data: dict[str, Any] | None
    error: str | None
    error_kind: str | None


@dataclass
class Ergebnis:
    """Ergebnis des Nachladens einer Kommune."""

    beratungen: int = 0
    vorlagen: int = 0
    fehlgeschlagen: int = 0
    abrufe: int = 0
    #: Kandidaten, die Kontingent, Wartezeit oder Abbruch für spätere Läufe lassen
    zurueckgestellt: int = 0
    #: Grund, aus dem das Nachladen vorzeitig endete (Sperre, Störung, Fensterende)
    abbruch: str | None = None

    def als_dict(self) -> dict[str, Any]:
        werte: dict[str, Any] = {
            "beratungen": self.beratungen,
            "vorlagen": self.vorlagen,
            "fehlgeschlagen": self.fehlgeschlagen,
            "abrufe": self.abrufe,
            "zurueckgestellt": self.zurueckgestellt,
        }
        if self.abbruch:
            werte["abbruch"] = self.abbruch
        return werte


def kontingent(sync_config: Mapping[str, Any] | None) -> int:
    """Abrufe je Lauf aus ``sync_config["beratungen_nachladen"]`` (ganze Zahl ≥ 0), sonst der Standard."""
    wert = (sync_config or {}).get(KONTINGENT_SCHLUESSEL)
    if isinstance(wert, bool) or not isinstance(wert, int) or wert < 0:
        return STANDARD_KONTINGENT
    return wert


def _stempel(wert: Any) -> datetime | None:
    if not isinstance(wert, str):
        return None
    try:
        stempel = datetime.fromisoformat(wert)
    except ValueError:
        return None
    return stempel if stempel.tzinfo is not None else stempel.replace(tzinfo=UTC)


def faellig(
    kandidaten: Sequence[tuple[str, datetime | None]],
    versuche: Mapping[str, Any],
    host: str,
    jetzt: datetime,
) -> list[str]:
    """
    Reihenfolge des Nachladens: Beratungen anstehender Sitzungen (früheste zuerst), dann vergangener (jüngste
    zuerst). Ohne Beratungen auf fremden Servern, synthetische Kennungen (``#``) und solche, deren letzter Versuch
    weniger als ``ERNEUT_NACH`` zurückliegt.
    """
    offen: list[tuple[bool, float, str]] = []
    for kennung, beginn in kandidaten:
        if not kennung or "#" in kennung or urlparse(kennung).netloc != host:
            continue
        zuletzt = _stempel(versuche.get(kennung))
        if zuletzt is not None and jetzt - zuletzt < ERNEUT_NACH:
            continue
        zeit = beginn.timestamp() if beginn is not None else jetzt.timestamp()
        vergangen = beginn is not None and beginn < jetzt
        offen.append((vergangen, -zeit if vergangen else zeit, kennung))
    return [kennung for _, _, kennung in sorted(offen)]


def _objekt(abruf: Abruf, typ: str, kennung: str) -> dict[str, Any] | None:
    """Objekt der Antwort, wenn es das erwartete ist (Typ, Kennung, nicht gelöscht), sonst ``None``."""
    daten = abruf.data
    if abruf.error or not isinstance(daten, dict) or is_oparl_error(daten):
        return None
    if not str(daten.get("type", "")).rstrip("/").endswith(f"/{typ}") or daten.get("id") != kennung:
        return None
    if daten.get("deleted") is True:
        return None
    return daten


def _verweis(wert: Any) -> str | None:
    if isinstance(wert, Mapping):
        wert = wert.get("id")
    return wert if isinstance(wert, str) and wert else None


async def nachladen(
    reihenfolge: Sequence[str],
    *,
    kontingent: int,
    jetzt: datetime,
    abrufen: Callable[[str], Awaitable[Abruf]],
    vorlage_bekannt: Callable[[str], Awaitable[bool]],
    speichern: Callable[[str, dict[str, Any]], Awaitable[bool]],
    weiter: Callable[[], bool],
) -> tuple[Ergebnis, dict[str, str]]:
    """
    Beratungen in ``reihenfolge`` einzeln laden und speichern, je Beratung höchstens zwei Abrufe (Beratung und
    unbekannte Vorlage, die Vorlage zuerst gespeichert). Rückgabe: Ergebnis und die Versuche, die zu merken sind
    (Kennung → Zeitpunkt; nur, was sich nicht laden oder speichern ließ).
    """
    ergebnis = Ergebnis()
    versuche: dict[str, str] = {}
    stempel = jetzt.astimezone(UTC).isoformat(timespec="seconds")
    for index, kennung in enumerate(reihenfolge):
        # Zwei Abrufe frei halten: Die Vorlage gehört zur Beratung
        if kontingent - ergebnis.abrufe < 2:
            ergebnis.zurueckgestellt = len(reihenfolge) - index
            break
        if not weiter():
            ergebnis.abbruch = "Abruffenster beendet"
            ergebnis.zurueckgestellt = len(reihenfolge) - index
            break
        abruf = await abrufen(kennung)
        ergebnis.abrufe += 1
        if abruf.error_kind is not None:
            # Sperre oder Störung des Servers: nicht weiter anfragen
            ergebnis.abbruch = abruf.error or abruf.error_kind
            ergebnis.fehlgeschlagen += 1
            ergebnis.zurueckgestellt = len(reihenfolge) - index - 1
            versuche[kennung] = stempel
            break
        beratung = _objekt(abruf, "Consultation", kennung)
        if beratung is None:
            ergebnis.fehlgeschlagen += 1
            versuche[kennung] = stempel
            continue
        vorlage = _verweis(beratung.get("paper"))
        if vorlage is not None and not await vorlage_bekannt(vorlage):
            vorlagen_abruf = await abrufen(vorlage)
            ergebnis.abrufe += 1
            daten = _objekt(vorlagen_abruf, "Paper", vorlage)
            if daten is not None and await _speichern(speichern, "paper", daten):
                ergebnis.vorlagen += 1
        if await _speichern(speichern, "consultation", beratung):
            ergebnis.beratungen += 1
        else:
            ergebnis.fehlgeschlagen += 1
            versuche[kennung] = stempel
    return ergebnis, versuche


async def _speichern(
    speichern: Callable[[str, dict[str, Any]], Awaitable[bool]], art: str, daten: dict[str, Any]
) -> bool:
    try:
        return await speichern(art, daten)
    except Exception as e:  # noqa: BLE001 - ein Objekt, das sich nicht speichern lässt, hält das Nachladen nicht auf
        console.print(f"[yellow]  Nachladen: {art} {daten.get('id')} nicht gespeichert ({e})[/yellow]")
        return False
