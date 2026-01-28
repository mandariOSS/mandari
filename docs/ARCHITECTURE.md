# Mandari 2.0 - Architektur

## Vision

Mandari ist eine Open-Source-Plattform für kommunalpolitische Transparenz. Sie ermöglicht Bürger:innen, politischen Organisationen und Verwaltungen den Zugang zu und die Arbeit mit kommunalpolitischen Daten.

## Die Drei Säulen

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           MANDARI PLATFORM                                   │
├─────────────────────┬─────────────────────┬─────────────────────────────────┤
│     SÄULE 1         │      SÄULE 2        │          SÄULE 3                │
│    PUBLIC           │      WORK           │          ADMIN                  │
│  (Transparenz)      │  (Organisationen)   │       (Verwaltung)              │
├─────────────────────┼─────────────────────┼─────────────────────────────────┤
│ • OParl-Daten       │ • Antragsdatenbank  │ • Ratsinformations-             │
│   durchsuchen       │ • Fraktionssitzungen│   system (RIS)                  │
│ • KI-Chatbot        │ • Mitgliedermgmt    │ • Sitzungsmanagement            │
│ • Politiker:innen   │ • Notizen & Kommen- │ • Aufwandsentschädi-            │
│   Fragen stellen    │   tare zu TOPs      │   gungen                        │
│ • Benachrichtigungen│ • Abstimmungs-      │ • Vorlagen & Anträge            │
│                     │   protokolle        │   erstellen                     │
│                     │ • Erinnerungen      │                                 │
├─────────────────────┴─────────────────────┴─────────────────────────────────┤
│                         SHARED SERVICES                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   API Core   │  │  Auth/Users  │  │   Search     │  │     AI       │     │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘     │
├─────────────────────────────────────────────────────────────────────────────┤
│                         DATA LAYER                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                    OParl Mirror (Lokale Kopie)                        │   │
│  │  PostgreSQL + JSON Storage                                            │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         INGESTOR                                      │   │
│  │  OParl API → Fetch → Validate → Store → Index                         │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Technologie-Stack

| Komponente | Technologie | Begründung |
|------------|-------------|------------|
| **Backend API** | FastAPI (Python) | Async, schnell, Type-Hints, OpenAPI auto-generated |
| **Ingestor** | Python + Optional Go | Python für Logik, Go für hohe Parallelisierung |
| **Datenbank** | PostgreSQL | JSONB für OParl-Rohdaten, relationale Daten für Domain |
| **Cache/Queue** | Redis | Task-Queue, Caching, Sessions |
| **Suche** | Meilisearch | Einfacher als OpenSearch, schnelle Full-Text-Suche |
| **Frontend** | Svelte/SvelteKit | Modern, leichtgewichtig, SSR-fähig |
| **AI/LLM** | Groq/OpenAI | Location-Extraktion, Summaries, Chatbot |

## Projektstruktur

```
mandari2.0/
├── apps/                          # Anwendungen
│   ├── api/                       # FastAPI Backend
│   │   ├── src/
│   │   │   ├── core/             # Core-Funktionalität
│   │   │   ├── oparl/            # OParl-Datenmodelle & Endpoints
│   │   │   ├── auth/             # Authentifizierung
│   │   │   ├── public/           # Säule 1: Public API
│   │   │   ├── work/             # Säule 2: Organizations API
│   │   │   ├── admin_ris/        # Säule 3: Verwaltungs-RIS (später)
│   │   │   ├── search/           # Suchfunktionalität
│   │   │   └── ai/               # KI-Services
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── ingestor/                  # OParl-Ingestor Service
│   │   ├── src/
│   │   │   ├── client/           # HTTP-Client für OParl
│   │   │   ├── sync/             # Synchronisationslogik
│   │   │   ├── storage/          # Datenspeicherung
│   │   │   └── scheduler/        # Zeitgesteuerte Syncs
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── web-public/                # Säule 1: Öffentliches Frontend
│   │   ├── src/
│   │   └── package.json
│   │
│   ├── web-work/                  # Säule 2: Organisations-Frontend
│   │   ├── src/
│   │   └── package.json
│   │
│   └── web-admin/                 # Säule 3: Verwaltungs-Frontend (später)
│       ├── src/
│       └── package.json
│
├── packages/                      # Shared Packages
│   ├── shared-types/              # TypeScript Types für Frontends
│   ├── shared-python/             # Python Shared Code
│   │   ├── mandari_shared/
│   │   │   ├── oparl/            # OParl-Schemas
│   │   │   ├── models/           # Shared Models
│   │   │   └── utils/            # Utilities
│   │   └── pyproject.toml
│   └── ui-components/             # Shared UI-Komponenten
│
├── infrastructure/                # DevOps
│   ├── docker/
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.dev.yml
│   │   └── services/
│   ├── kubernetes/               # (Optional für Production)
│   └── scripts/
│
├── docs/                          # Dokumentation
│   ├── ARCHITECTURE.md           # Diese Datei
│   ├── DEVELOPMENT.md            # Entwickler-Setup
│   ├── API.md                    # API-Dokumentation
│   ├── OPARL.md                  # OParl-Spezifikation
│   └── decisions/                # Architecture Decision Records
│       └── 001-fastapi.md
│
├── _old/                          # Alter Code (Referenz)
│
├── CLAUDE.md                      # Claude AI Context
├── README.md
├── pyproject.toml                 # Root Python Config (Monorepo)
└── package.json                   # Root Node Config (Monorepo)
```

## Datenarchitektur

### OParl-Mirror-Konzept

Die zentrale Idee: **Der Ingestor erstellt eine lokale 1:1-Kopie der OParl-Daten**.

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   OParl API     │     │    INGESTOR     │     │   PostgreSQL    │
│   (Stadt XY)    │────▶│                 │────▶│   (Mirror)      │
└─────────────────┘     │  • Fetch        │     │                 │
                        │  • Validate     │     │  • raw_json     │
┌─────────────────┐     │  • Transform    │     │  • normalized   │
│   OParl API     │────▶│  • Store        │     │  • indexed      │
│   (Stadt AB)    │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                               ┌─────────────────────────────────────┐
                               │           FRONTENDS                  │
                               │  • Nutzen IMMER lokale Kopie        │
                               │  • Keine direkten OParl-Calls       │
                               │  • Schnell & offline-fähig          │
                               └─────────────────────────────────────┘
```

### Datenbank-Schema (Vereinfacht)

```sql
-- OParl Core Tables (Mirror)
CREATE TABLE oparl_sources (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    url TEXT UNIQUE,
    last_sync TIMESTAMPTZ,
    config JSONB
);

CREATE TABLE oparl_bodies (
    id UUID PRIMARY KEY,
    external_id TEXT UNIQUE,  -- OParl URL
    source_id UUID REFERENCES oparl_sources,
    name VARCHAR(255),
    raw_json JSONB,           -- Komplettes OParl-Objekt
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

CREATE TABLE oparl_meetings (
    id UUID PRIMARY KEY,
    external_id TEXT UNIQUE,
    body_id UUID REFERENCES oparl_bodies,
    name VARCHAR(255),
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    location TEXT,
    cancelled BOOLEAN DEFAULT FALSE,
    raw_json JSONB,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

CREATE TABLE oparl_papers (
    id UUID PRIMARY KEY,
    external_id TEXT UNIQUE,
    body_id UUID REFERENCES oparl_bodies,
    name VARCHAR(255),
    reference VARCHAR(100),
    paper_type VARCHAR(100),
    raw_json JSONB,
    -- AI-Enhanced Fields
    summary TEXT,
    locations JSONB,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

-- Weitere: oparl_persons, oparl_organizations, oparl_files, oparl_agenda_items

-- Domain Tables (Säule 2: Work)
CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    type VARCHAR(50),  -- 'party', 'faction', 'group'
    parent_id UUID REFERENCES organizations,
    settings JSONB
);

CREATE TABLE organization_members (
    id UUID PRIMARY KEY,
    organization_id UUID REFERENCES organizations,
    user_id UUID REFERENCES users,
    role VARCHAR(50),
    joined_at TIMESTAMPTZ
);

CREATE TABLE motions (
    id UUID PRIMARY KEY,
    organization_id UUID REFERENCES organizations,
    title VARCHAR(255),
    content TEXT,
    status VARCHAR(50),
    linked_paper_id UUID REFERENCES oparl_papers,
    created_by UUID REFERENCES users,
    created_at TIMESTAMPTZ
);

-- Mehr: meeting_notes, agenda_comments, voting_records, etc.
```

## API-Design

### Öffentliche API (Säule 1)

```
GET  /api/v1/bodies                    # Liste aller Kommunen
GET  /api/v1/bodies/{id}               # Einzelne Kommune
GET  /api/v1/bodies/{id}/meetings      # Sitzungen einer Kommune
GET  /api/v1/bodies/{id}/papers        # Vorgänge einer Kommune
GET  /api/v1/meetings/{id}             # Einzelne Sitzung
GET  /api/v1/papers/{id}               # Einzelner Vorgang
GET  /api/v1/persons/{id}              # Einzelne Person
GET  /api/v1/search                    # Volltextsuche
POST /api/v1/ai/chat                   # KI-Chatbot
POST /api/v1/questions                 # Frage an Politiker:in
```

### Organisations-API (Säule 2, authentifiziert)

```
# Auth
POST /api/v1/auth/login
POST /api/v1/auth/logout
POST /api/v1/auth/2fa/setup

# Organization Management
GET  /api/v1/org/{org_id}
GET  /api/v1/org/{org_id}/members
POST /api/v1/org/{org_id}/members/invite

# Motions (Anträge)
GET  /api/v1/org/{org_id}/motions
POST /api/v1/org/{org_id}/motions
PATCH /api/v1/org/{org_id}/motions/{id}

# Meetings (Fraktionssitzungen)
GET  /api/v1/org/{org_id}/meetings
POST /api/v1/org/{org_id}/meetings
POST /api/v1/org/{org_id}/meetings/{id}/notes

# Agenda Comments (Notizen zu TOPs)
GET  /api/v1/org/{org_id}/agenda-comments
POST /api/v1/org/{org_id}/agenda-comments

# Notifications
GET  /api/v1/notifications
POST /api/v1/notifications/settings
```

## Ingestor-Architektur

```
┌─────────────────────────────────────────────────────────────────┐
│                        INGESTOR                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐          │
│  │  Scheduler  │───▶│   Fetcher   │───▶│  Processor  │          │
│  │  (APScheduler)   │  (httpx)    │    │             │          │
│  └─────────────┘    └─────────────┘    └─────────────┘          │
│         │                 │                   │                  │
│         │                 │                   ▼                  │
│         │                 │          ┌─────────────┐            │
│         │                 │          │  Validator  │            │
│         │                 │          │  (OParl)    │            │
│         │                 │          └─────────────┘            │
│         │                 │                   │                  │
│         │                 │                   ▼                  │
│         │                 │          ┌─────────────┐            │
│         │                 │          │   Storage   │            │
│         │                 │          │ (PostgreSQL)│            │
│         │                 │          └─────────────┘            │
│         │                 │                   │                  │
│         ▼                 ▼                   ▼                  │
│  ┌─────────────────────────────────────────────────┐            │
│  │              Event Bus (Redis Pub/Sub)           │            │
│  └─────────────────────────────────────────────────┘            │
│         │                                                        │
│         ▼                                                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Indexer    │  │  AI Worker  │  │  Notifier   │              │
│  │(Meilisearch)│  │  (Groq)     │  │  (Webhooks) │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Sync-Strategien

1. **Full Sync**: Kompletter Abruf aller Daten (initial oder Recovery)
2. **Incremental Sync**: Nur geänderte Objekte (via `modified` Timestamps)
3. **Delta Sync**: ETags + If-Modified-Since für Bandbreitenersparnis

## Sicherheit & Datenschutz

### Authentifizierung

- **Public (Säule 1)**: Optional, für Bookmarks und Benachrichtigungen
- **Work (Säule 2)**: Pflicht, mit 2FA für sensible Aktionen
- **Admin (Säule 3)**: Pflicht + Rollenbasierte Zugriffskontrolle

### DSGVO-Compliance

- Audit-Logging für alle schreibenden Operationen
- Daten-Export auf Anfrage
- Lösch-Workflows
- Anonymisierung nach Aufbewahrungsfristen

## Deployment-Optionen

### Development
```yaml
# docker-compose.dev.yml
services:
  postgres:
    image: postgres:16
  redis:
    image: redis:7
  meilisearch:
    image: getmeili/meilisearch:latest
  api:
    build: ./apps/api
    volumes:
      - ./apps/api:/app
  ingestor:
    build: ./apps/ingestor
  web-public:
    build: ./apps/web-public
    ports:
      - "3000:3000"
```

### Production
- Container-Orchestrierung (Docker Compose oder Kubernetes)
- Managed PostgreSQL (z.B. Supabase, AWS RDS)
- Managed Redis (z.B. Upstash)
- CDN für statische Assets
- Reverse Proxy (Caddy/Nginx)

## Nächste Schritte

1. **Phase 1: Foundation**
   - [x] Architektur-Dokumentation
   - [ ] Projektstruktur anlegen
   - [ ] Backend-Grundgerüst (FastAPI)
   - [ ] Ingestor-Basis
   - [ ] Datenbank-Schema

2. **Phase 2: Core Features**
   - [ ] OParl-Sync vollständig
   - [ ] Public API
   - [ ] Public Frontend (Basic)
   - [ ] Suche

3. **Phase 3: Säule 2**
   - [ ] Auth-System
   - [ ] Organizations
   - [ ] Motions
   - [ ] Work Frontend

4. **Phase 4: AI & Polish**
   - [ ] KI-Chatbot
   - [ ] Location-Extraktion
   - [ ] Summaries
   - [ ] Notifications
