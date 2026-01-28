# Entwickler-Dokumentation

## Entwicklungsumgebung einrichten

### Voraussetzungen

- **Python 3.12+** - [python.org](https://python.org)
- **Node.js 22+** - [nodejs.org](https://nodejs.org)
- **Docker** - [docker.com](https://docker.com)
- **uv** - [astral.sh/uv](https://docs.astral.sh/uv/)
- **pnpm** - [pnpm.io](https://pnpm.io/)

### Installation (Windows)

```powershell
# Python (via winget)
winget install Python.Python.3.12

# Node.js (via winget)
winget install OpenJS.NodeJS.LTS

# uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# pnpm
npm install -g pnpm

# Docker Desktop
winget install Docker.DockerDesktop
```

### Installation (macOS/Linux)

```bash
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# pnpm
npm install -g pnpm
```

## Projekt-Setup

### 1. Infrastruktur starten

```bash
# Nur Datenbanken und Services (empfohlen für Entwicklung)
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d

# Mit Management-Tools (pgAdmin, Redis Commander)
docker compose -f infrastructure/docker/docker-compose.dev.yml --profile tools up -d
```

**Services:**
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`
- Meilisearch: `localhost:7700`
- pgAdmin (optional): `localhost:5050`
- Redis Commander (optional): `localhost:8081`

### 2. Backend (API)

```bash
cd apps/api

# Dependencies installieren
uv sync

# .env erstellen
cp ../../.env.example .env

# Server starten
uv run uvicorn src.main:app --reload --port 8000
```

**Endpoints:**
- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### 3. Ingestor

```bash
cd apps/ingestor

# Dependencies installieren
uv sync

# Status prüfen
uv run python -m src.main status

# OParl-Quelle hinzufügen
uv run python -m src.main add-source --url "https://example.oparl.org/oparl/v1"

# Daten synchronisieren
uv run python -m src.main sync --all
```

### 4. Frontend (Public)

```bash
cd apps/web-public

# Dependencies installieren
pnpm install

# Dev-Server starten
pnpm dev
```

Frontend: http://localhost:3000

### 5. Frontend (Work)

```bash
cd apps/web-work

# Dependencies installieren
pnpm install

# Dev-Server starten
pnpm dev
```

Frontend: http://localhost:3001

## Datenbank

### Migrationen

```bash
cd apps/api

# Neue Migration erstellen
uv run alembic revision --autogenerate -m "beschreibung"

# Migrationen ausführen
uv run alembic upgrade head

# Migration zurücksetzen
uv run alembic downgrade -1
```

### Datenbank zurücksetzen

```bash
# Container stoppen und Volume löschen
docker compose -f infrastructure/docker/docker-compose.dev.yml down -v

# Neu starten
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d
```

## Tests

### Backend-Tests

```bash
cd apps/api

# Alle Tests
uv run pytest

# Mit Coverage
uv run pytest --cov=src

# Einzelne Datei
uv run pytest tests/test_oparl.py -v
```

### Frontend-Tests

```bash
cd apps/web-public

# Type-Check
pnpm check

# Lint
pnpm lint
```

## Code-Qualität

### Python

```bash
cd apps/api

# Formatting
uv run ruff format .

# Linting
uv run ruff check .

# Type-Checking
uv run mypy src/
```

### TypeScript/Svelte

```bash
cd apps/web-public

# Formatting
pnpm format

# Linting
pnpm lint

# Type-Check
pnpm check
```

## Debugging

### VS Code Launch-Konfiguration

Erstelle `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "API: Debug",
      "type": "debugpy",
      "request": "launch",
      "module": "uvicorn",
      "args": ["src.main:app", "--reload"],
      "cwd": "${workspaceFolder}/apps/api",
      "env": {
        "DEBUG": "true"
      }
    },
    {
      "name": "Ingestor: Sync",
      "type": "debugpy",
      "request": "launch",
      "module": "src.main",
      "args": ["sync", "--all"],
      "cwd": "${workspaceFolder}/apps/ingestor"
    }
  ]
}
```

## Architektur-Entscheidungen

Siehe [docs/decisions/](decisions/) für Architecture Decision Records (ADRs).

## Troubleshooting

### Port bereits belegt

```bash
# Prozess auf Port finden (Windows)
netstat -ano | findstr :8000

# Prozess auf Port finden (Linux/macOS)
lsof -i :8000
```

### Docker-Probleme

```bash
# Container-Logs anzeigen
docker compose -f infrastructure/docker/docker-compose.dev.yml logs -f

# Alles neu starten
docker compose -f infrastructure/docker/docker-compose.dev.yml down
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d
```

### Python-Umgebungsprobleme

```bash
# Cache löschen
uv cache clean

# Virtuelle Umgebung neu erstellen
rm -rf .venv
uv sync
```
