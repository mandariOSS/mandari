"""
Scraper-Adapter für Nicht-OParl-Ratsinformationssysteme.

Adapter erzeugen synthetisches OParl-1.1-JSON und speisen es in die
unveränderte Pipeline (OParlProcessor -> DatabaseStorage-Upserts -> OCR ->
Elasticsearch -> Tombstones) ein. Siehe docs/SCRAPER_SOURCES.md.

Quellen-Auswahl über OParlSource.sync_config["source_type"]:
  - "oparl" (Default)          -> normaler OParl-Sync
  - "bridge:allris"            -> normaler OParl-Sync (externer oparl-bridge-
                                  Proxy, nur Provenienz-Label)
  - "scraper:sessionnet"       -> SessionNetAdapter (dieses Paket)
"""

from src.scrapers.base import ScraperAdapter, ScraperConfig, content_hash

SCRAPER_SOURCE_PREFIX = "scraper:"


def get_adapter(source_type: str, config: "ScraperConfig", fetcher) -> "ScraperAdapter":
    """Liefert den Adapter für einen scraper:*-source_type."""
    vendor = source_type.removeprefix(SCRAPER_SOURCE_PREFIX)
    if vendor == "sessionnet":
        from src.scrapers.sessionnet import SessionNetAdapter

        return SessionNetAdapter(config, fetcher)
    raise ValueError(f"Unbekannter Scraper-Vendor: {vendor!r} (source_type={source_type!r})")


__all__ = [
    "SCRAPER_SOURCE_PREFIX",
    "ScraperAdapter",
    "ScraperConfig",
    "content_hash",
    "get_adapter",
]
