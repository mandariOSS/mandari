"""
Basis-Bausteine des Scraper-Frameworks.

- ScraperConfig: Konfiguration je Quelle aus OParlSource.sync_config["scraper"]
- CrawlWindow: Zeitfenster für den Sitzungskalender
- ScraperAdapter: Protokoll, das jeder Vendor-Adapter erfüllt
- external_id-Normalisierung (kanonische Detail-URLs)
- content_hash: Feld-Diffing über kanonisiertes JSON
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Volatile Felder, die NICHT in den Content-Hash eingehen (ändern sich bei
# jedem Crawl bzw. sind der Hash selbst).
VOLATILE_HASH_FIELDS = ("modified", "created", "mandari:contentHash")

CONTENT_HASH_FIELD = "mandari:contentHash"


@dataclass
class CrawlWindow:
    """Zeitfenster für Listen-Crawls (Sitzungskalender)."""

    start: date
    end: date

    @classmethod
    def from_days(cls, days_back: int, days_ahead: int, today: date | None = None) -> CrawlWindow:
        today = today or date.today()
        return cls(start=today + timedelta(days=days_back), end=today + timedelta(days=days_ahead))

    def months(self) -> list[tuple[int, int]]:
        """Alle (Jahr, Monat)-Paare im Fenster, chronologisch."""
        result: list[tuple[int, int]] = []
        year, month = self.start.year, self.start.month
        while (year, month) <= (self.end.year, self.end.month):
            result.append((year, month))
            month += 1
            if month > 12:
                month = 1
                year += 1
        return result


@dataclass
class ScraperConfig:
    """
    Adapter-Konfiguration je Quelle, gelesen aus
    OParlSource.sync_config["scraper"] (JSONB, additiv — keine Migration).
    """

    base_url: str
    body_name: str = "Unbekannte Kommune"
    # "asp" | "php" — Seiten-Endung der SessionNet-Installation.
    # None = Auto-Detect aus base_url-Probe.
    variant: str | None = None
    rate_limit_seconds: float = 2.0
    max_concurrent: int = 1
    # Kalender-Fenster inkrementeller Läufe: [heute-60d, heute+210d]
    calendar_window_days: tuple[int, int] = (-60, 210)
    # Fenster für Full-Crawls (Historie)
    full_window_days: tuple[int, int] = (-365, 210)
    # Obergrenze für Detailseiten-Fetches je Lauf (Politeness/Pilot);
    # None = unbegrenzt
    max_detail_pages: int | None = None
    # Gremien-Mitglieder (kp0040) nur im Full-Crawl abrufen
    members_on_full_only: bool = True
    adapter_schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sync_config(cls, sync_config: dict[str, Any] | None) -> ScraperConfig:
        raw = dict((sync_config or {}).get("scraper") or {})
        base_url = raw.get("base_url") or ""
        if not base_url:
            raise ValueError(
                "sync_config['scraper']['base_url'] fehlt — ohne Basis-URL kein Crawl."
            )
        window = raw.get("calendar_window_days") or (-60, 210)
        full_window = raw.get("full_window_days") or (-365, 210)
        known = {
            "base_url",
            "body_name",
            "variant",
            "rate_limit_seconds",
            "max_concurrent",
            "calendar_window_days",
            "full_window_days",
            "max_detail_pages",
            "members_on_full_only",
            "adapter_schema_version",
        }
        return cls(
            base_url=base_url if base_url.endswith("/") else base_url + "/",
            body_name=raw.get("body_name") or "Unbekannte Kommune",
            variant=raw.get("variant"),
            rate_limit_seconds=float(raw.get("rate_limit_seconds", 2.0)),
            max_concurrent=int(raw.get("max_concurrent", 1)),
            calendar_window_days=(int(window[0]), int(window[1])),
            full_window_days=(int(full_window[0]), int(full_window[1])),
            max_detail_pages=(
                int(raw["max_detail_pages"]) if raw.get("max_detail_pages") else None
            ),
            members_on_full_only=bool(raw.get("members_on_full_only", True)),
            adapter_schema_version=int(raw.get("adapter_schema_version", 1)),
            extra={k: v for k, v in raw.items() if k not in known},
        )


class ScraperAdapter(Protocol):
    """
    Protokoll für Vendor-Adapter.

    Ein Adapter liefert einen Strom synthetischer OParl-1.1-Dicts mit
    stabilen id-URLs (kanonische Detailseiten-URLs des Vendors). Die
    Reihenfolge der Entity-Typen muss FK-kompatibel sein:
    organization -> person -> membership -> paper -> meeting -> consultation.
    """

    vendor: str
    schema_version: int

    def build_body(self) -> dict[str, Any]:
        """Synthetisches OParl-Body-Dict der Quelle."""
        ...

    def iter_entities(
        self, window: CrawlWindow, full: bool
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        """Yield (entity_type, Seite von OParl-Dicts) in FK-Reihenfolge."""
        ...

    @property
    def stats(self) -> ScrapeStats:
        """Parse-Statistik des laufenden Crawls."""
        ...


@dataclass
class ScrapeStats:
    """Parse-Quoten-Statistik eines Crawl-Laufs."""

    pages_fetched: int = 0
    detail_pages_attempted: int = 0
    detail_pages_parsed: int = 0
    parse_failures: int = 0
    entities_parsed: int = 0

    @property
    def parse_quota(self) -> float:
        """Anteil erfolgreich geparster Detailseiten (1.0 wenn keine)."""
        if self.detail_pages_attempted == 0:
            return 1.0
        return self.detail_pages_parsed / self.detail_pages_attempted


# ---------------------------------------------------------------------------
# external_id-Normalisierung
# ---------------------------------------------------------------------------


def normalize_external_id(url: str, keep_params: tuple[str, ...]) -> str:
    """
    Normalisiert eine Detailseiten-URL zur kanonischen external_id:
    https erzwingen, Host lowercase, nur ID-tragende Query-Parameter,
    Parameter sortiert, kein Fragment.
    """
    parsed = urlparse(url)
    scheme = "https"
    netloc = parsed.netloc.lower()
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = [(k, v) for k, v in pairs if k in keep_params]
    params.sort()
    return urlunparse((scheme, netloc, parsed.path, "", urlencode(params), ""))


# ---------------------------------------------------------------------------
# Content-Hash (Feld-Diffing)
# ---------------------------------------------------------------------------


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_HASH_FIELDS
        }
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def content_hash(entity: dict[str, Any]) -> str:
    """
    sha256 über das kanonisierte JSON (ohne volatile Felder).

    Der Hash landet als "mandari:contentHash" im synthetischen Dict und
    damit im raw_json der DB — der Runner upsertet nur bei Differenz.
    """
    canonical = json.dumps(
        _strip_volatile(entity), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def with_content_hash(entity: dict[str, Any]) -> dict[str, Any]:
    """Ergänzt das Dict um seinen Content-Hash (idempotent)."""
    entity[CONTENT_HASH_FIELD] = content_hash(entity)
    return entity
