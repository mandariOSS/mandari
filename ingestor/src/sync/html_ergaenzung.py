# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergänzung von OParl-Objekten aus den Detailseiten des RIS (Issue #1431).

Manche OParl-Schnittstellen liefern die Struktur, aber nicht die Inhalte: ALLRIS 4 (Herzogenrath) lässt
``resolutionText``, Abstimmung und Wortprotokoll der Tagesordnungspunkte und die Texte der Vorlagen leer. Mit
``sync_config["html_ergaenzung"] = "allris4"`` liest der Abgleich für Tagesordnungspunkte (eigene Liste und in
Sitzungen eingebettet) und Vorlagen die Detailseite, auf die ``web`` zeigt, und ergänzt die Felder, die OParl leer
lässt (``src/scrapers/allris4.py``). Was die Quelle selbst liefert, hat immer Vorrang.

Abrufe:

- Je Objekt höchstens eine Seite, und nur, wenn sich das Objekt seit der letzten Ergänzung geändert hat. Die
  Ergänzung steht mit einem Vermerk im Rohdatensatz (``mandari:htmlErgaenzung``: Art, Seite, ``modified`` des
  Objekts beim Abruf, ergänzte Felder). Bei jedem weiteren Upsert übernimmt der Abgleich die gespeicherte Ergänzung
  aus dem Bestand, statt die Seite neu zu laden: So schreiben eingebettete und gelistete Tagesordnungspunkte
  denselben Rohdatensatz, und der Abgleich meldet keine Scheinänderungen. Neu geladen wird nur, wenn ``modified``
  des Objekts neuer ist als beim Abruf und die Stempel verlässlich sind (``timestamps_unreliable``: ALLRIS 4
  stempelt ältere Punkte mit der Uhrzeit des Abrufs).
- Höchstens ``html_ergaenzung_je_lauf`` Seiten je Lauf (Standard ``DEFAULT_PAGES_PER_RUN``); was darüber liegt,
  bleibt für spätere Läufe (der wöchentliche Vollabgleich holt den Bestand so nach und nach nach). Mit dem Ende
  des Abruffensters (``fetch_deadline``) lädt die Ergänzung keine Seite mehr.
- Nur Seiten auf dem Host der Quelle und nur die bekannten Detailseiten (``to020?TOLFDNR=``, ``vo020?VOLFDNR=``).
  Takt, Kennung und robots.txt wie beim OParl-Abruf (``OParlClient.fetch_page``).

Ein Fehler beim Laden oder Lesen einer Seite bricht den Abgleich nicht ab: Das Objekt behält die gespeicherte
Ergänzung (oder bleibt ohne), der Lauf zählt den Fehlschlag (``HtmlErgaenzung.stats``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from src.client.oparl_compat import timestamps_unreliable
from src.redaction import MaskingConsole
from src.scrapers.allris4 import parse_agenda_item_page, parse_paper_page

console = MaskingConsole()

#: Vermerk der Ergänzung im Rohdatensatz des Objekts
MARKER_KEY = "mandari:htmlErgaenzung"
#: Bekannte Arten (``sync_config["html_ergaenzung"]``)
ART_ALLRIS4 = "allris4"
KNOWN_ARTS = frozenset({ART_ALLRIS4})
#: Seiten je Lauf ohne eigene Angabe der Quelle
DEFAULT_PAGES_PER_RUN = 1500
MAX_PAGES_PER_RUN = 100_000

#: Felder, die die Ergänzung je Objektart schreiben kann
AGENDA_ITEM_FIELDS = ("resolutionText", "mandari:vote", "mandari:minutes")
PAPER_FIELDS = ("mandari:proposal", "mandari:explanation")
FIELDS = {"agendaitem": AGENDA_ITEM_FIELDS, "paper": PAPER_FIELDS}

#: Detailseiten von ALLRIS 4: Pfadende und Kennungsparameter je Objektart
_ALLRIS4_PAGES = {"agendaitem": ("/to020", "TOLFDNR"), "paper": ("/vo020", "VOLFDNR")}


class PageResult(Protocol):
    status_code: int
    error: str | None
    text: str | None


#: Lädt eine Seite (``OParlClient.fetch_page``)
FetchPage = Callable[[str], Awaitable[PageResult]]
#: Liest die gespeicherten Ergänzungen: Objektart, Kennungen -> Kennung -> Vermerk und Felder
LoadStored = Callable[[str, list[str]], Awaitable[dict[str, dict[str, Any]]]]


@dataclass(frozen=True)
class HtmlErgaenzungConfig:
    """``html_ergaenzung`` und ``html_ergaenzung_je_lauf`` aus der ``sync_config`` einer Quelle."""

    art: str
    pages_per_run: int = DEFAULT_PAGES_PER_RUN

    @classmethod
    def parse(cls, sync_config: Any) -> HtmlErgaenzungConfig | None:
        """Gültige Angabe der Quelle, sonst ``None`` (keine Ergänzung)."""
        if not isinstance(sync_config, Mapping):
            return None
        art = sync_config.get("html_ergaenzung")
        if not isinstance(art, str) or art.strip().lower() not in KNOWN_ARTS:
            return None
        limit = sync_config.get("html_ergaenzung_je_lauf")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_PAGES_PER_RUN:
            limit = DEFAULT_PAGES_PER_RUN
        return cls(art=art.strip().lower(), pages_per_run=limit)


@dataclass
class ErgaenzungStats:
    """Zählung eines Laufs."""

    loaded: int = 0  # Seiten geladen und gelesen
    reused: int = 0  # gespeicherte Ergänzung übernommen
    failed: int = 0  # Seite nicht ladbar oder nicht lesbar
    deferred: int = 0  # Grenze des Laufs oder Fensterende erreicht, später
    skipped: int = 0  # ohne passende Seite (kein ``web``, fremder Host, andere Seite)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "geladen": self.loaded,
            "uebernommen": self.reused,
            "fehlgeschlagen": self.failed,
            "verschoben": self.deferred,
            "ohne_seite": self.skipped,
        }


def detail_page(entity_type: str, web: Any, hosts: Iterable[str]) -> str | None:
    """Adresse der Detailseite, wenn ``web`` eine bekannte Seite von ALLRIS 4 auf einem der ``hosts`` ist."""
    if not isinstance(web, str) or not web.startswith(("http://", "https://")):
        return None
    page = _ALLRIS4_PAGES.get(entity_type)
    if page is None:
        return None
    parts = urlparse(web)
    if parts.netloc.lower() not in {host.lower() for host in hosts}:
        return None
    suffix, id_param = page
    if not parts.path.endswith(suffix):
        return None
    values = parse_qs(parts.query).get(id_param)
    if not values or not values[0].isdigit():
        return None
    return web


class HtmlErgaenzung:
    """Ergänzung eines Laufs für eine Quelle (siehe Moduldokumentation)."""

    def __init__(
        self,
        config: HtmlErgaenzungConfig,
        *,
        hosts: Iterable[str],
        fetch_page: FetchPage,
        load_stored: LoadStored,
        deadline_passed: Callable[[], bool] = lambda: False,
    ) -> None:
        self.config = config
        self.hosts = frozenset(host.lower() for host in hosts if host)
        self._fetch_page = fetch_page
        self._load_stored = load_stored
        self._deadline_passed = deadline_passed
        self.stats = ErgaenzungStats()
        # Begonnene Abrufe (auch laufende): Sitzungen und Vorlagen laufen parallel, das Kontingent wird deshalb vor
        # dem Abruf ohne dazwischenliegendes ``await`` belegt
        self._started = 0

    @property
    def budget_left(self) -> int:
        return max(0, self.config.pages_per_run - self._started)

    async def ergaenze(self, entity_type: str, item: dict[str, Any]) -> None:
        """
        Objekt vor dem Verarbeiten ergänzen: Vorlagen und Tagesordnungspunkte selbst, bei Sitzungen die eingebetteten
        Tagesordnungspunkte. Andere Objektarten bleiben unverändert.
        """
        if entity_type == "meeting":
            targets = [entry for entry in item.get("agendaItem") or [] if isinstance(entry, dict)]
            kind = "agendaitem"
        elif entity_type in FIELDS:
            targets, kind = [item], entity_type
        else:
            return
        ids = [str(entry["id"]) for entry in targets if isinstance(entry.get("id"), str) and entry["id"]]
        if not ids:
            return
        stored = await self._load_stored(kind, ids)
        for entry in targets:
            external_id = entry.get("id")
            if isinstance(external_id, str) and external_id:
                await self._one(kind, entry, stored.get(external_id))

    async def _one(self, kind: str, data: dict[str, Any], stored: Mapping[str, Any] | None) -> None:
        marker = stored.get(MARKER_KEY) if stored else None
        marker = marker if isinstance(marker, Mapping) and marker.get("art") == self.config.art else None
        if marker is not None and not self._changed(data, marker):
            self._reuse(kind, data, stored, marker)
            self.stats.reused += 1
            return
        page = detail_page(kind, data.get("web"), self.hosts)
        if page is None:
            self.stats.skipped += 1
            if marker is not None:
                self._reuse(kind, data, stored, marker)
            return
        if self.budget_left <= 0 or self._deadline_passed():
            self.stats.deferred += 1
            if marker is not None:
                self._reuse(kind, data, stored, marker)
            return
        self._started += 1
        fields = await self._load(kind, page, data)
        if fields is None:
            self.stats.failed += 1
            if marker is not None:
                self._reuse(kind, data, stored, marker)
            return
        self.stats.loaded += 1
        added: list[str] = []
        for key in FIELDS[kind]:
            value = fields.get(key)
            if value and not data.get(key):
                data[key] = value
                added.append(key)
        data[MARKER_KEY] = {"art": self.config.art, "seite": page, "stand": data.get("modified"), "felder": added}

    @staticmethod
    def _changed(data: Mapping[str, Any], marker: Mapping[str, Any]) -> bool:
        """Hat sich das Objekt seit der Ergänzung geändert? Unzuverlässige Stempel zählen nicht als Änderung."""
        if timestamps_unreliable(dict(data)):
            return False
        return data.get("modified") != marker.get("stand")

    @staticmethod
    def _reuse(kind: str, data: dict[str, Any], stored: Mapping[str, Any] | None, marker: Mapping[str, Any]) -> None:
        """Gespeicherte Ergänzung übernehmen, soweit die Quelle das Feld nicht selbst liefert."""
        felder = marker.get("felder")
        for key in felder if isinstance(felder, list) else []:
            if key in FIELDS[kind] and not data.get(key) and stored and stored.get(key):
                data[key] = stored[key]
        data[MARKER_KEY] = dict(marker)

    async def _load(self, kind: str, page: str, data: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            result = await self._fetch_page(page)
        except Exception as exc:  # noqa: BLE001 – eine Seite darf den Abgleich nicht abbrechen
            self._note_error(f"{page}: {exc}")
            return None
        if result.error or result.status_code != 200 or not result.text:
            self._note_error(f"{page}: {result.error or f'HTTP {result.status_code}'}")
            return None
        try:
            if kind == "agendaitem":
                fields = parse_agenda_item_page(result.text, data.get("result"))
            else:
                fields = parse_paper_page(result.text)
        except Exception as exc:  # noqa: BLE001
            self._note_error(f"{page}: nicht lesbar ({exc})")
            return None
        if fields is None:
            self._note_error(f"{page}: keine Detailseite von ALLRIS 4")
        return fields

    def _note_error(self, message: str) -> None:
        if len(self.stats.errors) < 20:
            self.stats.errors.append(message)
        console.print(f"[yellow]  Ergänzung: {message}[/yellow]")

    def summary(self) -> str:
        s = self.stats
        return (
            f"Ergänzung aus Detailseiten: {s.loaded} geladen, {s.reused} übernommen, {s.failed} fehlgeschlagen, "
            f"{s.deferred} verschoben, {s.skipped} ohne Seite"
        )
