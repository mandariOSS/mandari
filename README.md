# Mandari

**Open-Source-Plattform für kommunalpolitische Transparenz**

Mandari ermöglicht Bürger:innen, politischen Organisationen und Verwaltungen den Zugang zu und die Arbeit mit kommunalpolitischen Daten basierend auf dem [OParl-Standard](https://oparl.org).

## Die Drei Säulen

| Säule | Beschreibung | Status |
|-------|--------------|--------|
| **Public** | Transparenzportal für Bürger:innen | In Entwicklung |
| **Work** | Portal für Parteien & Fraktionen | In Entwicklung |
| **Admin** | Vollständiges RIS für Verwaltungen | Geplant |

## Schnellstart

### Voraussetzungen

- Python 3.12+
- Node.js 22+
- Docker & Docker Compose
- [uv](https://github.com/astral-sh/uv) (Python Package Manager)
- [pnpm](https://pnpm.io/) (Node Package Manager)

### 1. Repository klonen

```bash
git clone https://github.com/your-org/mandari.git
cd mandari
```

### 2. Infrastruktur starten

```bash
# Startet PostgreSQL, Redis und Meilisearch
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d
```

### 3. Backend starten

```bash
cd apps/api
cp ../../.env.example .env
uv sync
uv run uvicorn src.main:app --reload
```

API verfügbar unter: http://localhost:8000/docs

### 4. Frontend starten

```bash
# Public Frontend (Port 3000)
cd apps/web-public
pnpm install
pnpm dev

# Work Frontend (Port 3001)
cd apps/web-work
pnpm install
pnpm dev
```

### 5. OParl-Daten synchronisieren

```bash
cd apps/ingestor
uv sync
uv run python -m src.main add-source --url "https://example.oparl.org/oparl/v1"
uv run python -m src.main sync --all
```

## Projektstruktur

```
mandari/
├── apps/
│   ├── api/              # FastAPI Backend
│   ├── ingestor/         # OParl Synchronisation
│   ├── web-public/       # Bürger:innen-Portal (SvelteKit)
│   ├── web-work/         # Organisations-Portal (SvelteKit)
│   └── web-admin/        # Verwaltungs-RIS (geplant)
├── packages/
│   └── shared-python/    # Gemeinsamer Python-Code
├── infrastructure/
│   └── docker/           # Docker Compose Konfiguration
├── docs/                 # Dokumentation
└── _old/                 # Alter Code (nur Referenz)
```

## Dokumentation

- [Architektur](docs/ARCHITECTURE.md) - Systemarchitektur und Design-Entscheidungen
- [Entwicklung](docs/DEVELOPMENT.md) - Setup und Entwickler-Guide
- [API](docs/API.md) - API-Dokumentation
- [OParl](docs/OPARL.md) - OParl-Spezifikation und Mapping

## Technologie-Stack

| Bereich | Technologie |
|---------|-------------|
| Backend | FastAPI (Python 3.12+) |
| Frontend | SvelteKit 2.0 |
| Datenbank | PostgreSQL 16 |
| Cache | Redis 7 |
| Suche | Meilisearch |
| AI | Groq / OpenAI |

## Features

### Säule 1: Public (Transparenz)

- OParl-Daten durchsuchen (Sitzungen, Vorlagen, Personen)
- Volltextsuche über alle Dokumente
- KI-Chatbot für Fragen zu kommunalen Themen
- Fragen an Politiker:innen stellen (wie AbgeordnetenWatch)
- Benachrichtigungen für neue Vorlagen

### Säule 2: Work (Organisationen)

- Antragsdatenbank für Fraktionen
- Fraktionssitzungen planen und dokumentieren
- Notizen und Kommentare zu Tagesordnungspunkten
- Abstimmungsprotokolle
- Mitgliederverwaltung
- Erinnerungen und Benachrichtigungen

### Säule 3: Admin (Verwaltung) - Geplant

- Vollständiges Ratsinformationssystem
- Sitzungsmanagement
- Aufwandsentschädigungen
- Vorlagen und Anträge erstellen

## Mitwirken

Wir freuen uns über Beiträge! Bitte lies unseren [Contributing Guide](CONTRIBUTING.md).

## Lizenz

Dieses Projekt steht unter der [AGPL-3.0 Lizenz](LICENSE).

## Links

- [OParl-Spezifikation](https://oparl.org/spezifikation/)
- [OParl-Endpunkte](https://dev.oparl.org/api/bodies)
