# Mandari

**Open-Source-Plattform für kommunalpolitische Transparenz in Deutschland**

Mandari ermöglicht Bürger:innen, politischen Organisationen und Verwaltungen den Zugang zu kommunalpolitischen Daten basierend auf dem [OParl-Standard](https://oparl.org).

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

## Die Drei Portale

| Portal | Beschreibung | Status |
|--------|--------------|--------|
| **Public** | Transparenzportal für Bürger:innen | In Entwicklung |
| **Work** | Arbeitsbereich für Fraktionen & Parteien | In Entwicklung |
| **Session** | Ratsinformationssystem für Verwaltungen | Geplant |

## Projektstruktur

```
mandari2.0/
├── mandari/                    # Django Hauptprojekt
│   ├── apps/                   # Django Apps
│   │   ├── accounts/           # Authentifizierung & 2FA
│   │   ├── common/             # Shared Utilities
│   │   ├── session/            # Verwaltungs-RIS
│   │   ├── tenants/            # Multi-Tenant & RBAC
│   │   └── work/               # Fraktions-Arbeitsbereich
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

## Schnellstart (Entwicklung)

### Voraussetzungen

- Python 3.12+
- Docker & Docker Compose
- [uv](https://github.com/astral-sh/uv) (Python Package Manager)

### 1. Repository klonen

```bash
git clone https://github.com/your-org/mandari.git
cd mandari
```

### 2. Infrastruktur starten

```bash
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d
```

### 3. Django Backend starten

```bash
cd mandari
cp .env.example .env
uv sync
uv run python manage.py migrate
uv run python manage.py setup_roles
uv run python manage.py runserver
```

### 4. OParl-Daten synchronisieren

```bash
cd apps/ingestor
uv sync
uv run python -m src.main sync --full
```

## Production Deployment

### Voraussetzungen

- 2x Hetzner VMs (Ubuntu 24.04)
- 1x Hetzner Load Balancer
- Domain mit DNS auf Load Balancer IP
- SSH-Zugang zu beiden Servern

### 1. Server einrichten

```bash
cd infrastructure/scripts
chmod +x setup-mandari.sh
./setup-mandari.sh
```

Das Script:
- Installiert Docker auf beiden Servern
- Konfiguriert Caddy, PostgreSQL, Redis, Meilisearch
- Generiert sichere Passwörter
- Erstellt alle Konfigurationsdateien

### 2. GitHub Secrets einrichten

Nach dem Setup werden alle benötigten Secrets angezeigt.

Gehe zu: `Repository Settings → Secrets and variables → Actions`

**Secrets hinzufügen:**

| Secret | Beschreibung |
|--------|--------------|
| `MASTER_IP` | Public IP des Master-Servers |
| `SLAVE_IP` | Public IP des Slave-Servers |
| `SSH_PRIVATE_KEY` | Inhalt von `~/.ssh/id_hetzner` |
| `POSTGRES_PASSWORD` | Vom Script generiert |
| `SECRET_KEY` | Vom Script generiert |
| `ENCRYPTION_MASTER_KEY` | Vom Script generiert |
| `MEILISEARCH_KEY` | Vom Script generiert |
| `SITE_URL` | `https://mandari.de` |

**Variable hinzufügen:**

| Variable | Wert |
|----------|------|
| `DEPLOYMENT_ENABLED` | `true` |

### 3. Deployment auslösen

```bash
git push origin main
```

Oder manuell: `Actions → Deploy Mandari → Run workflow`

### Nützliche Befehle

```bash
# Status prüfen
ssh root@MASTER_IP 'cd /opt/mandari && docker compose ps'

# Logs anzeigen
ssh root@MASTER_IP 'cd /opt/mandari && docker compose logs -f api'

# Django Shell
ssh root@MASTER_IP 'docker exec -it mandari-api python manage.py shell'

# Datenbank-Backup
ssh root@MASTER_IP 'docker exec mandari-postgres pg_dump -U mandari mandari > backup.sql'
```

## OParl Ingestor

Der Ingestor synchronisiert OParl-Daten von deutschen Ratsinformationssystemen.

### Features

- **Inkrementelle Syncs** - Nur geänderte Daten (alle 15 Min)
- **Full Syncs** - Komplette Synchronisation (täglich 3 Uhr)
- **Redis Event Emission** - Real-time Events für neue Sitzungen/Vorlagen
- **Prometheus Metrics** - Monitoring auf Port 9090
- **Circuit Breaker** - Automatische Fehlertoleranz

### Befehle

```bash
cd apps/ingestor

# Einmalige Synchronisation
uv run python -m src.main sync --full

# Daemon-Modus (für Production)
uv run python -m src.main daemon --interval 15 --metrics-port 9090

# Status anzeigen
uv run python -m src.main status

# Metriken anzeigen
uv run python -m src.main metrics
```

## Features

### Portal: Public (Transparenz)

- OParl-Daten durchsuchen (Sitzungen, Vorlagen, Personen)
- Volltextsuche über alle Dokumente
- KI-Zusammenfassungen von Vorlagen
- Kartenansicht mit Geo-Referenzen

### Portal: Work (Organisationen)

- Fraktionssitzungen planen und dokumentieren
- Notizen zu Tagesordnungspunkten
- Anträge erstellen und verwalten
- Aufgabenverwaltung
- Mitgliederverwaltung mit RBAC
- Verschlüsselte sensible Daten (AES-256)

### Portal: Session (Verwaltung) - Geplant

- Vollständiges Ratsinformationssystem
- Sitzungsmanagement
- Vorlagen und Anträge erstellen
- Aufwandsentschädigungen

## Sicherheit

- **Multi-Tenant Isolation** - Strikte Datentrennung pro Organisation
- **RBAC** - 50+ feingranulare Berechtigungen
- **2FA** - TOTP-basierte Zwei-Faktor-Authentifizierung
- **Verschlüsselung** - AES-256-GCM für sensible Daten
- **Rate Limiting** - Schutz vor Brute-Force
- **Audit Trail** - Vollständige Nachverfolgbarkeit

## Mitwirken

Wir freuen uns über Beiträge! Siehe [CONTRIBUTING.md](CONTRIBUTING.md).

## Lizenz

[AGPL-3.0](LICENSE)

## Links

- [OParl-Spezifikation](https://oparl.org/spezifikation/)
- [OParl-Endpunkte](https://dev.oparl.org/api/bodies)
