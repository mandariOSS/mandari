# Software Bill of Materials (SBOM)

> **Hinweis:** Dies ist eine **vereinfachte, manuell gepflegte SBOM** der direkten
> Abhängigkeiten des Mandari-Monorepos (Django-Hauptprojekt `mandari/`,
> OParl-Ingestor `ingestor/`, Shared-Package `shared/`). Transitive Abhängigkeiten
> sind nicht vollständig aufgeführt. Eine **maschinenlesbare CycloneDX-Generierung
> ist geplant: TODO via [syft](https://github.com/anchore/syft) in CI.**
>
> Quellen: [`mandari/pyproject.toml`](../mandari/pyproject.toml),
> [`mandari/requirements.txt`](../mandari/requirements.txt),
> [`ingestor/pyproject.toml`](../ingestor/pyproject.toml),
> [`shared/pyproject.toml`](../shared/pyproject.toml).
> Siehe auch [DEPENDENCIES.md](../DEPENDENCIES.md) (Danksagungen).

**Projekt:** Mandari — Open-Source-Plattform für kommunalpolitische Transparenz
**Lizenz des Projekts:** AGPL-3.0
**Sprache/Runtime:** Python ≥ 3.12
**Stand:** Juli 2026

---

## 1. Django-Hauptprojekt (`mandari/`)

### Framework & Server

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| Django | ≥ 6.0 | BSD-3-Clause | Web-Framework (Backend, Templates, ORM, Background Tasks) |
| django-htmx | ≥ 1.19.0 | MIT | HTMX-Integration für Django |
| django-unfold | ≥ 0.40.0 | MIT | Modernes Admin-Theme |
| whitenoise | ≥ 6.7.0 | MIT | Static-File-Serving in Produktion |
| gunicorn | ≥ 23.0.0 | MIT | WSGI-Produktionsserver |
| daphne | ≥ 4.1.0 | BSD-3-Clause | ASGI-Server für WebSockets |
| channels | ≥ 4.2.0 | BSD-3-Clause | WebSocket-/Echtzeit-Unterstützung |
| channels-redis | ≥ 4.2.0 | BSD-3-Clause | Redis-Channel-Layer für Channels |
| django-safemigrate | ≥ 4.3 | BSD-3-Clause | Zero-Downtime-Migrationen |

### Datenbank & Cache

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| psycopg[binary] | ≥ 3.2.0 | LGPL-3.0 | PostgreSQL-Adapter |
| dj-database-url | ≥ 2.2.0 | BSD-3-Clause | Datenbank-Konfiguration via URL |
| sqlalchemy | ≥ 2.0.36 | MIT | DB-Zugriff für Ingestor-Integration (Sync-Daemon) |
| asyncpg | ≥ 0.30.0 | Apache-2.0 | Asynchroner PostgreSQL-Treiber |
| redis | ≥ 5.2.0 | MIT | Redis-Client (Cache, Sessions, Queues) |
| django-redis | ≥ 5.4.0 | BSD-3-Clause | Redis-Cache-Backend für Django |

### Suche & KI

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| elasticsearch | ≥ 8.12.0, < 9 | Apache-2.0 | Volltextsuche (Python-Client) |
| httpx | ≥ 0.28.0 | BSD-3-Clause | HTTP-Client (KI-Anbindung, OParl-Abrufe) |

### Dokumente & OCR

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| pypdf | ≥ 6.4.0 | BSD-3-Clause | PDF-Textextraktion |
| pytesseract | ≥ 0.3.10 | Apache-2.0 | OCR (Tesseract-Anbindung) |
| pdf2image | ≥ 1.16.0 | MIT | PDF → Bild-Konvertierung für OCR |
| xhtml2pdf | ≥ 0.2.17 | Apache-2.0 | PDF-Generierung aus HTML |
| reportlab | ≥ 4.0.0 | BSD-3-Clause | PDF-Generierung (Low-Level) |
| python-docx | ≥ 1.1.0 | MIT | DOCX-Generierung |
| pillow | ≥ 10.3.0 | MIT-CMU (HPND) | Bildverarbeitung (Logos, OCR-Vorverarbeitung) |

### Utilities & Sicherheit

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| cryptography | ≥ 44.0.0 | Apache-2.0 / BSD-3-Clause | Feldverschlüsselung (AES-256-GCM, tenant-spezifisch) |
| pydantic-settings | ≥ 2.6.0 | MIT | Typisierte Konfiguration |
| python-dotenv | ≥ 1.0.0 | BSD-3-Clause | .env-Konfiguration |
| python-dateutil | ≥ 2.9.0 | Apache-2.0 / BSD-3-Clause | Datums-Utilities |
| markdown | ≥ 3.7 | BSD-3-Clause | Markdown-Rendering (Template-Filter) |
| rich | ≥ 13.9.0 | MIT | Konsolen-Ausgabe (Management-Commands) |
| mandari-oparl (`shared/`) | 0.1.0.dev0 | AGPL-3.0 | Eigenes Shared-Package: OParl-Pydantic-Schemas |

### Transitive Abhängigkeiten (aus Sicherheitsgründen gepinnt)

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| urllib3 | ≥ 2.5.0 | MIT | HTTP (Fix: Open Redirect, Info Disclosure) |
| requests | ≥ 2.32.4 | Apache-2.0 | HTTP (Fix: Control-Flow-, Datenleck-Issues) |
| anyio | ≥ 4.4.0 | MIT | Async-Kompatibilität (Fix: Race Condition) |
| zipp | ≥ 3.19.1 | MIT | Zip-Handling (Fix: Infinite Loop) |

---

## 2. OParl-Ingestor (`ingestor/`)

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| mandari-oparl (`shared/`) | 0.1.0.dev0 | AGPL-3.0 | Shared OParl-Pydantic-Schemas |
| httpx | ≥ 0.28.0 | BSD-3-Clause | Asynchrone OParl-API-Abrufe |
| pydantic | ≥ 2.10.0 | MIT | Datenvalidierung (OParl-Entitäten) |
| pydantic-settings | ≥ 2.6.0 | MIT | Typisierte Konfiguration |
| sqlalchemy | ≥ 2.0.36 | MIT | Datenbank-ORM |
| asyncpg | ≥ 0.30.0 | Apache-2.0 | Asynchroner PostgreSQL-Treiber |
| redis | ≥ 5.2.0 | MIT | Queues & Status |
| typer | ≥ 0.15.0 | MIT | CLI-Framework |
| rich | ≥ 13.9.0 | MIT | Konsolen-Ausgabe |
| apscheduler | ≥ 3.10.0 | MIT | Zeitgesteuerte Sync-Jobs |
| prometheus-client | ≥ 0.21.0 | Apache-2.0 | Metriken/Monitoring |
| aiohttp | ≥ 3.11.0 | Apache-2.0 | HTTP (Monitoring & Resilience) |
| pypdf | ≥ 4.0.0 | BSD-3-Clause | PDF-Textextraktion |
| pytesseract | ≥ 0.3.10 | Apache-2.0 | OCR |
| pdf2image | ≥ 1.17.0 | MIT | PDF → Bild-Konvertierung |

---

## 3. Shared-Package (`shared/` — `mandari-oparl`)

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| pydantic | ≥ 2.10.0 | MIT | OParl-Typen als Pydantic-Schemas |

---

## 4. Infrastruktur & externe Dienste (nicht pip-installiert)

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| PostgreSQL | 16 | PostgreSQL License | Relationale Datenbank |
| Redis | 7 | BSD-3-Clause | Cache, Sessions, Channel-Layer |
| Elasticsearch | 8 | SSPL / Elastic License 2.0 | Volltextsuche |
| Caddy | 2 | Apache-2.0 | Reverse Proxy mit automatischem TLS |
| Docker / Docker Compose | — | Apache-2.0 | Container-Plattform |
| Tesseract OCR | — | Apache-2.0 | OCR-Engine (System-Abhängigkeit von pytesseract) |
| Poppler | — | GPL-2.0/-3.0 | PDF-Rendering (System-Abhängigkeit von pdf2image) |

## 5. Frontend-Bibliotheken (via Templates/Static)

| Komponente | Version | Lizenz | Zweck |
|---|---|---|---|
| HTMX | — | BSD-2-Clause | Hypermedia-Framework für dynamische UIs |
| Alpine.js | — | MIT | Leichtgewichtige Client-Logik |
| Tailwind CSS | — | MIT | Utility-First CSS |
| Lucide Icons | — | ISC | Icon-Set |
| MapLibre GL | — | BSD-3-Clause | Karten-Rendering |
| Chart.js | — | MIT | Diagramme |
| FullCalendar | — | MIT | Kalender-Komponente |
| EasyMDE | — | MIT | Markdown-Editor |

---

*Dev-Abhängigkeiten (pytest, ruff, mypy u. a.) sind nicht Teil der Auslieferung
und daher hier nicht aufgeführt. Vollständige, versionsgenaue Auflösung inkl.
transitiver Pakete: siehe TODO oben (CycloneDX via syft in CI).*
