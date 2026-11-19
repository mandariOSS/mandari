# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremiennamen nicht veröffentlichter Gremien aus dem öffentlichen Sitzungskalender (SessionNet).

Manche OParl-Schnittstellen nennen an einer Sitzung ein Gremium, geben es aber nicht heraus: Münster antwortet für
die Interfraktionelle Arbeitsgruppe Internationales, Aufsichtsräte und Beiräte mit HTTP 401. Die Sitzung steht dann
ohne Gremium im Bestand und heißt „Sitzung eines nicht veröffentlichten Gremiums“ (Issue #1441), obwohl der öffentliche
Kalender der Stadt (SessionNet ``si0040``) dieselbe Sitzung mit dem Namen des Gremiums zeigt.

Mit ``sync_config["sessionnet_kalender"] = "https://…/sessionnetbi/"`` (Adresse des Bürgerinfo-Frontends) liest
der Abgleich nach den Sitzungen einer Kommune für genau diese Sitzungen den Kalender des jeweiligen Monats und legt
den Namen als ``mandari:gremiumWeb`` (Name, Seite, Abrufzeit) im Rohdatensatz ab; die Anzeige nimmt ihn statt des
Platzhalters. Die Sitzungsnummer im Kalender (``__ksinr``) ist bei SessionNet das letzte Glied der OParl-Kennung.

Abrufe: nur Sitzungen ohne zugeordnetes Gremium und ohne gespeicherten Namen, nur im Zeitraum ``ZURUECK`` bis
``VORAUS``, eine Kalenderseite je Monat und höchstens ``MONATE_JE_LAUF`` Monate je Lauf, Takt, Kennung und
robots.txt wie beim OParl-Abruf (``OParlClient.fetch_page``). Überschreibt ein späterer Abgleich die Sitzung, fehlt
der Name wieder und der nächste Lauf liest ihn neu. Fehler beim Laden oder Lesen halten den Abgleich nicht auf.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from src.redaction import MaskingConsole
from src.scrapers.sessionnet import is_calendar_page, parse_calendar

console = MaskingConsole()

#: Ablage im Rohdatensatz der Sitzung
SCHLUESSEL = "mandari:gremiumWeb"
#: Zeitraum, für den der Kalender gelesen wird (vergangene Sitzungen ändern sich selten, ferne gibt es noch nicht)
ZURUECK = timedelta(days=62)
VORAUS = timedelta(days=400)
MONATE_JE_LAUF = 6
ZONE = ZoneInfo("Europe/Berlin")


class Seite(Protocol):
    status_code: int
    error: str | None
    text: str | None


@dataclass(frozen=True)
class KalenderConfig:
    """``sessionnet_kalender`` aus der ``sync_config`` einer Quelle: Adresse des Bürgerinfo-Frontends."""

    basis: str
    endung: str = "php"

    @classmethod
    def parse(cls, sync_config: Any) -> KalenderConfig | None:
        if not isinstance(sync_config, Mapping):
            return None
        basis = sync_config.get("sessionnet_kalender")
        if not isinstance(basis, str) or not basis.startswith(("http://", "https://")) or not urlparse(basis).netloc:
            return None
        endung = sync_config.get("sessionnet_kalender_endung")
        return cls(basis=basis if basis.endswith("/") else basis + "/", endung="asp" if endung == "asp" else "php")

    def monat(self, jahr: int, monat: int) -> str:
        return f"{self.basis}si0040.{self.endung}?__cjahr={jahr}&__cmonat={monat}"


@dataclass(frozen=True)
class OhneGremium:
    """Sitzung im Bestand, deren Gremium die Quelle nennt, aber nicht herausgibt."""

    id: Any
    external_id: str
    start: datetime


@dataclass
class KalenderStats:
    seiten: int = 0
    benannt: int = 0
    ohne_eintrag: int = 0
    fehlgeschlagen: int = 0
    verschoben: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "seiten": self.seiten,
            "benannt": self.benannt,
            "ohne_eintrag": self.ohne_eintrag,
            "fehlgeschlagen": self.fehlgeschlagen,
            "verschoben": self.verschoben,
        }


def sitzungsnummer(external_id: str) -> int | None:
    """``__ksinr`` aus der OParl-Kennung einer SessionNet-Sitzung (letztes Glied, nur Ziffern)."""
    letztes = external_id.rstrip("/").rsplit("/", 1)[-1]
    return int(letztes) if letztes.isdigit() else None


def namen_im_kalender(html: str) -> dict[int, str]:
    """Sitzungsnummer → Name des Gremiums laut Monatskalender; ohne lesbaren Kalender leer."""
    if not is_calendar_page(html):
        return {}
    namen: dict[int, str] = {}
    for eintrag in parse_calendar(html):
        name = (eintrag.committee or eintrag.name or "").strip()
        if name and name.lower() != "sitzung":
            namen[eintrag.ksinr] = name
    return namen


LadeOhneGremium = Callable[[datetime, datetime], Awaitable[list[OhneGremium]]]
Speichere = Callable[[Any, dict[str, str]], Awaitable[None]]
LadeSeite = Callable[[str], Awaitable[Seite]]


async def ergaenzen(
    config: KalenderConfig,
    lade_ohne_gremium: LadeOhneGremium,
    lade_seite: LadeSeite,
    speichere: Speichere,
    *,
    jetzt: datetime | None = None,
    frist_vorbei: Callable[[], bool] = lambda: False,
) -> KalenderStats:
    """Namen für die Sitzungen ohne Gremium aus dem Kalender ergänzen, Monat für Monat, nächste Monate zuerst."""
    stats = KalenderStats()
    jetzt = jetzt or datetime.now(UTC)
    offen = await lade_ohne_gremium(jetzt - ZURUECK, jetzt + VORAUS)
    je_monat: dict[tuple[int, int], list[OhneGremium]] = defaultdict(list)
    for sitzung in offen:
        if sitzungsnummer(sitzung.external_id) is None:
            continue
        ortszeit = sitzung.start.astimezone(ZONE)
        je_monat[(ortszeit.year, ortszeit.month)].append(sitzung)
    heute = jetzt.astimezone(ZONE)
    # Kommende Monate zuerst (dort sucht man), dann die vergangenen
    reihenfolge = sorted(
        je_monat, key=lambda m: (m < (heute.year, heute.month), abs(m[0] * 12 + m[1] - heute.year * 12 - heute.month))
    )
    for nummer, (jahr, monat) in enumerate(reihenfolge):
        sitzungen = je_monat[(jahr, monat)]
        if nummer >= MONATE_JE_LAUF or frist_vorbei():
            stats.verschoben += len(sitzungen)
            continue
        url = config.monat(jahr, monat)
        try:
            seite = await lade_seite(url)
            if seite.error or seite.status_code != 200 or not seite.text:
                raise ValueError(seite.error or f"HTTP {seite.status_code}")
            namen = namen_im_kalender(seite.text)
            stats.seiten += 1
        except Exception as exc:  # noqa: BLE001 - eine Seite hält den Abgleich nicht auf
            stats.fehlgeschlagen += len(sitzungen)
            stats.errors.append(f"{url}: {exc}")
            continue
        abgerufen = jetzt.isoformat()
        for sitzung in sitzungen:
            name = namen.get(sitzungsnummer(sitzung.external_id) or -1)
            if not name:
                stats.ohne_eintrag += 1
                continue
            await speichere(sitzung.id, {"name": name, "seite": url, "abgerufen": abgerufen})
            stats.benannt += 1
    return stats
