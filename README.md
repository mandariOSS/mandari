# Mandari

**Open-Source-Plattform für kommunalpolitische Transparenz in Deutschland**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://github.com/mandariOSS/mandari/pkgs/container/mandari)

Mandari macht Kommunalpolitik transparent, verständlich und zugänglich. Wir glauben: **Demokratie braucht Transparenz** - und die Werkzeuge dafür sollten allen zur Verfügung stehen.

Kommunalpolitik betrifft uns alle unmittelbar - von der Kita-Planung über die Verkehrsführung bis zur Stadtentwicklung. Doch allzu oft sind diese wichtigen Entscheidungsprozesse für Bürger:innen schwer nachvollziehbar. Das wollen wir ändern.

## Quick Start (Community Edition)

```bash
# Clone repository
git clone https://github.com/mandariOSS/mandari.git
cd mandari

# Run interactive installer
./install.sh
```

The installer guides you through configuration and starts all services automatically.

**Requirements**: Linux server with Docker & Docker Compose

See [Installation Guide](docs/installation.md) for details.

## Die Drei Säulen

Mandari besteht aus drei Modulen - alle 100% Open Source unter AGPL-3.0:

### Mandari Insight - Bürger:innen-Portal

**Kostenlos, ohne Anmeldung, ohne Tracker**

Das Transparenzportal für alle. Bürger:innen, Journalist:innen und Initiativen können Ratsinformationen durchsuchen, verstehen und nachverfolgen.

- Volltextsuche über alle Sitzungen, Vorlagen und Dokumente
- KI-Zusammenfassungen komplexer Vorlagen
- Interaktiver KI-Chatbot für Fragen
- Kartenansicht - was passiert in meiner Nachbarschaft?
- Abstimmungsverhalten von Politiker:innen einsehen
- Keine Cookies, kein Tracking, kein Login erforderlich

### Mandari Work - Fraktions-Plattform

**Professionelle Ratsarbeit für Teams**

Das Kollaborationstool für Fraktionen, Gruppen und Einzelmandatsträger:innen. Effiziente Sitzungsvorbereitung, Teamabstimmung und Wissensmanagement.

- Persönliche Dashboards mit anstehenden Sitzungen
- Fraktionssitzungen planen und dokumentieren
- Notizen und Kommentare zu Tagesordnungspunkten teilen
- Interne Abstimmungen vor Gremiensitzungen
- Antragsdatenbank mit Vorlagen und Versionierung
- KI-gestützte Recherche und Zusammenfassungen
- Aufgabenverwaltung und Erinnerungen
- Rollenbasierte Berechtigungen (50+ feingranulare Rechte)
- Verschlüsselte sensible Daten (AES-256-GCM)

### Mandari Session - Ratsinformationssystem

**Das offene RIS für Verwaltungen** *(In Entwicklung)*

Vollständiges Sitzungsmanagement mit OParl-Export - die Open-Source-Alternative zu proprietären RIS-Lösungen.

- Sitzungsplanung und Tagesordnungserstellung
- Automatischer Einladungsversand
- Vorlagen- und Dokumentenverwaltung
- Protokollierung und Beschlussverfolgung
- Sitzungsgeld und Aufwandsentschädigung
- OParl-Export für maximale Transparenz

## Warum Mandari?

### Kein Vendor Lock-in

Anders als proprietäre RIS-Lösungen setzen wir auf ein **offenes Ökosystem**:

- **Vollständiger Datenexport** jederzeit (JSON, CSV, OParl)
- **Offene REST-API** für eigene Integrationen
- **OParl-Standard** für Import und Export
- **100% Open Source** - der Code gehört der Gemeinschaft

### OParl-kompatibel

Mandari basiert auf dem [OParl-Standard](https://oparl.org) - dem deutschen Standard für offene Ratsinformationssysteme. Über 100 Kommunen bieten bereits OParl-Schnittstellen an, darunter:

- ALLRIS
- regisafe
- Somacos
- SessionNet
- und viele mehr

Mandari Work funktioniert mit jedem RIS, das OParl unterstützt - Sie müssen nicht wechseln.

### Für wen wir arbeiten

Mandari richtet sich an **demokratische politische Akteur:innen**:

| Zielgruppe | Produkt | Beschreibung |
|------------|---------|--------------|
| **Bürger:innen** | Insight | Kostenloser Zugang zu Ratsinformationen |
| **Fraktionen & Parteien** | Work | Professionelle Kollaboration |
| **Verwaltungen** | Session | Vollständiges Sitzungsmanagement |
| **Journalist:innen** | Insight | Recherche-Tool für Lokalpolitik |
| **Forschung** | API | Maschinenlesbare Daten |

Wir verstehen uns als Teil der demokratischen Zivilgesellschaft und behalten uns vor, mit wem wir zusammenarbeiten.

## Architektur

```
                         ┌─────────────────────────────────┐
                         │       Load Balancer             │
                         │       (mandari.de)              │
                         └─────────────┬───────────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
                    ▼                                     ▼
         ┌───────────────────┐              ┌───────────────────┐
         │      Master       │              │       Slave       │
         │                   │              │                   │
         │  • Caddy          │              │  • Caddy          │
         │  • Django API     │              │  • Django API     │
         │  • PostgreSQL     │              │  • PostgreSQL     │
         │  • Redis          │              │  • Redis          │
         │  • Meilisearch    │              │  • Meilisearch    │
         │  • Ingestor       │              │                   │
         └───────────────────┘              └───────────────────┘
```

## Technologie-Stack

| Bereich | Technologie |
|---------|-------------|
| Backend | Django 6.0 (Python 3.12+) |
| Frontend | Django Templates + HTMX + Alpine.js |
| CSS | Tailwind CSS |
| Datenbank | PostgreSQL 16 |
| Cache | Redis 7 |
| Suche | Meilisearch |
| Reverse Proxy | Caddy |
| Container | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| KI | Groq / OpenAI (optional) |

## Projektstruktur

```
mandari2.0/
├── mandari/                    # Django Hauptprojekt
│   ├── apps/                   # Django Apps
│   │   ├── accounts/           # Authentifizierung & 2FA
│   │   ├── common/             # Shared Utilities & Encryption
│   │   ├── session/            # Verwaltungs-RIS (Session)
│   │   ├── tenants/            # Multi-Tenant & RBAC
│   │   └── work/               # Fraktions-Arbeitsbereich
│   │       ├── dashboard/      # Übersicht
│   │       ├── faction/        # Fraktionssitzungen
│   │       ├── meetings/       # RIS-Sitzungsvorbereitung
│   │       ├── motions/        # Anträge
│   │       ├── tasks/          # Aufgaben
│   │       └── ris/            # RIS-Datenansicht
│   ├── insight_core/           # OParl-Datenmodelle
│   ├── insight_sync/           # OParl-Synchronisation
│   ├── insight_search/         # Suchfunktionalität
│   └── insight_ai/             # KI-Pipelines
├── apps/
│   └── ingestor/               # Standalone OParl-Ingestor
├── infrastructure/
│   ├── scripts/                # Setup & Deployment Scripts
│   └── docker/                 # Docker Compose Configs
└── docs/                       # Dokumentation
```

## Installation

### Community Edition (Single Server)

For a single-server deployment:

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install.sh
```

**Requirements**:
- Linux server (Ubuntu 22.04+ recommended)
- Docker & Docker Compose
- Domain with DNS pointing to server (for HTTPS)

See [Installation Guide](docs/installation.md) for detailed instructions.

### Development Setup

For local development:

```bash
# Start infrastructure services
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d

# Setup Django backend
cd mandari
cp .env.example .env
uv sync
uv run python manage.py migrate
uv run python manage.py setup_roles
uv run python manage.py runserver

# In another terminal: Sync OParl data
cd apps/ingestor
uv sync
uv run python -m src.main sync --full
```

**Requirements**:
- Python 3.12+
- Docker & Docker Compose
- [uv](https://github.com/astral-sh/uv) (Python Package Manager)

See [Development Guide](docs/development.md) for details.

### Management Commands

```bash
# View service status
docker compose ps

# View logs
docker compose logs -f api

# Create backup
./backup.sh

# Update to latest version
./update.sh

# Django shell
docker exec -it mandari-api python manage.py shell
```

## OParl Ingestor

Der Ingestor synchronisiert OParl-Daten von deutschen Ratsinformationssystemen.

### Features

- **Inkrementelle Syncs** - Nur geänderte Daten (alle 15 Min)
- **Full Syncs** - Komplette Synchronisation (täglich 3 Uhr)
- **Redis Event Emission** - Real-time Events für neue Sitzungen/Vorlagen
- **Prometheus Metrics** - Monitoring auf Port 9090
- **Circuit Breaker** - Automatische Fehlertoleranz bei API-Ausfällen

### Befehle

```bash
cd apps/ingestor

# Einmalige Synchronisation
uv run python -m src.main sync --full

# Daemon-Modus (für Production)
uv run python -m src.main daemon --interval 15 --metrics-port 9090

# Status anzeigen
uv run python -m src.main status
```

## Sicherheit

- **Multi-Tenant Isolation** - Strikte Datentrennung pro Organisation
- **RBAC** - 50+ feingranulare Berechtigungen in 14 Kategorien
- **2FA** - TOTP-basierte Zwei-Faktor-Authentifizierung
- **Verschlüsselung** - AES-256-GCM für sensible Daten (tenant-spezifische Keys)
- **Rate Limiting** - Schutz vor Brute-Force (5 Versuche/15 Min)
- **Audit Trail** - Vollständige Nachverfolgbarkeit
- **DSGVO-konform** - Hosting in Deutschland, keine Tracker

## Unsere Werte

- **Transparenz** - Offene Prozesse, nachvollziehbare Entscheidungen
- **Teilhabe** - Politische Beteiligung für alle
- **Offenheit** - 100% Open Source, kein Vendor Lock-in
- **Datenschutz** - DSGVO-konform, minimale Datenerhebung
- **Inklusion** - Barrierefreies Design (WCAG 2.1 AA)
- **Vielfalt** - Gendergerechte Sprache, offene Community

## Mitwirken

Wir freuen uns über Beiträge! Mandari lebt von Menschen, die unsere Werte teilen - ob als Entwickler:in, Kommune oder Unterstützer:in.

Siehe [CONTRIBUTING.md](CONTRIBUTING.md) für Details.

## Lizenz

[AGPL-3.0](LICENSE) - Der gesamte Quellcode ist frei verfügbar. Sie können Mandari selbst hosten oder unseren Managed Service nutzen.

## Links

- [OParl-Spezifikation](https://oparl.org/spezifikation/)
- [OParl-Endpunkte](https://dev.oparl.org/api/bodies)
- [GitHub Repository](https://github.com/mandari-oss/mandari)
