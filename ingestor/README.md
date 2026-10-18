# Mandari Ingestor

Der Ingestor gleicht die OParl-Schnittstellen der Ratsinformationssysteme mit der Datenbank von mandari ab
(Daemon: `python -m src.main daemon`) und liest Quellen ohne OParl über Scraper. Jede Quelle hat dabei ihren
eigenen Takt nach Bedarf (Arbeitszeit, Sitzungstage, Ruhephasen; `src/scheduler/takt.py`).

- Wann, wie oft, mit welcher Kennung und in welchem Takt er abruft, steht in
  [`docs/ABRUFREGELN.md`](../docs/ABRUFREGELN.md).
- Quellen anbinden, Scraper und die Einstellungen je Quelle (`sync_config`):
  [`docs/SCRAPER_SOURCES.md`](../docs/SCRAPER_SOURCES.md).
