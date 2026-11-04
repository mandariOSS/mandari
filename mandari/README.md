# mandari: Django-Anwendung

Dieses Verzeichnis enthält die Django-Anwendung von mandari: Bürgerportal (Insight), Arbeitsbereich für
Fraktionen (Work) und Verwaltungs-RIS (Session) in einem Projekt. Den Abgleich mit den
Ratsinformationssystemen übernimmt der Ingestor (`../ingestor/`, eigener Container); gemeinsame Pakete für
beide liegen in `../shared/` (OParl-Typen, Texterkennung).

## Inhalt

| Verzeichnis | Inhalt |
|-------------|--------|
| `mandari/` | Projekt: Einstellungen (`settings.py`, `settings_test.py`), URLs, ASGI |
| `apps/` | Fachmodule `session`, `work` und `minutes` (Protokollierung); Plattform `accounts`, `tenants`, `common`, `events` (Ereignisse, Aufträge, Zeitpläne, Worker) und `provisioning` |
| `hub/` | Datendrehscheibe: Verträge, Befehle, RIS-Modell, Sichten, Adapter, offene OParl-Schnittstelle |
| `insight_core/` | OParl-Datenmodelle, Views und Services des Bürgerportals |
| `insight_search/`, `insight_ai/`, `insight_sync/` | Elasticsearch-Anbindung, KI-Funktionen, Steuerung des Abgleichs |
| `templates/`, `static/`, `frontend/` | Django-Templates, statische Dateien, TypeScript-Quellen (Vite) |
| `tests_e2e/` | End-to-End-Tests im Browser (Playwright, axe-core) |
| `Dockerfile` | Image `ghcr.io/mandarioss/mandari`, gebaut mit der Repository-Wurzel als Kontext |

Abhängigkeiten zeigen nur nach unten (Fachmodule → Drehscheibe → Plattform); `lint-imports` prüft das,
siehe [`../docs/ENGINEERING_STANDARDS.md`](../docs/ENGINEERING_STANDARDS.md).

## Technik

| Bereich | Stand |
|---------|-------|
| Python | 3.12 bis 3.14 (`pyproject.toml`), Image auf `python:3.14-slim` |
| Web-Framework | Django 6.1, ASGI mit Daphne und Channels |
| Datenbank | PostgreSQL 16 |
| Cache, Sessions, WebSocket | Redis 7 |
| Suche | Elasticsearch 8 |
| Frontend | Django-Templates, HTMX, Alpine.js, Tailwind CSS, Vite |
| Admin | django-unfold |
| Abhängigkeiten | `pyproject.toml`, gesperrt in `uv.lock`; `package.json`, `package-lock.json` |

Hintergrundarbeit erledigt `manage.py events_worker` in den Compose-Diensten `worker`, `worker-heavy` und
`worker-live` ([`../DEPLOYMENT.md`](../DEPLOYMENT.md), „Worker“).

KI-Funktionen haben keinen festen Anbieter: Anbieter, Modell und Schlüssel stehen im Admin unter
„KI-Einstellungen“, aufgerufen werden nur Hosts aus `KI_ERLAUBTE_HOSTS` (leer: KI aus).

## Entwicklung und Tests

Einrichtung, Tests und statische Prüfungen beschreibt [`../CONTRIBUTING.md`](../CONTRIBUTING.md). Die
häufigsten Befehle gibt es auch als `make`-Ziele in der Repository-Wurzel (`make help`).

## Betrieb

Installation, Aktualisierung und Sicherung: [`../README.md`](../README.md) und
[`../DEPLOYMENT.md`](../DEPLOYMENT.md). Quellen anbinden und Abrufregeln:
[`../docs/ABRUFREGELN.md`](../docs/ABRUFREGELN.md), Ingestor: [`../ingestor/README.md`](../ingestor/README.md).

## Lizenz

AGPL-3.0-or-later – siehe [LICENSE](../LICENSE)
