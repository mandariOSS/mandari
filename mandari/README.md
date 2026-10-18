# Mandari Insight

**Kommunalpolitische Transparenz mit KI-Unterstützung**

Mandari Insight ist eine Open-Source-Plattform für die Darstellung und Analyse von Ratsinformationssystemen (RIS) auf Basis des OParl-Standards.

## Technologie-Stack

- **Django 5.2+** - Web Framework mit Server-Side Rendering
- **PostgreSQL 15+** - Datenbank
- **Elasticsearch** - Volltextsuche
- **Redis** - Caching und Sessions
- **OpenAI-kompatibler KI-Anbieter (Vorlage oder eigener Endpunkt)** - KI-Features (Zusammenfassungen, Chatbot),
  eingerichtet im Admin unter „KI-Einstellungen“, nur Hosts aus `KI_ERLAUBTE_HOSTS`
- **HTMX** - Interaktivität ohne JavaScript-Framework
- **Alpine.js** - Leichtgewichtiges JavaScript
- **TailwindCSS** - Utility-first Styling

## Features

- **Gremien**: Aktive und vergangene Gremien mit Mitgliedern
- **Personen**: Ratsmitglieder mit Kontaktdaten und Mitgliedschaften
- **Vorgänge**: Vorlagen und Beschlüsse mit KI-Zusammenfassungen
- **Termine**: Sitzungen als Liste und Kalender
- **Suche**: Volltextsuche über alle Daten
- **Karte**: Geo-Visualisierung von Vorgängen
- **KI-Chat**: Fragen zu kommunalpolitischen Themen

## Installation

### Voraussetzungen

- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Elasticsearch (optional)

### Setup

```bash
# Repository klonen
cd mandari

# Virtuelle Umgebung (.venv) mit den gesperrten Versionen aus uv.lock, samt Entwicklungswerkzeugen
uv sync --extra dev
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows

# .env Datei erstellen
cp ../.env.example ../.env

# Datenbank migrieren
python manage.py migrate

# Superuser erstellen
python manage.py createsuperuser

# Entwicklungsserver starten
python manage.py runserver
```

### Umgebungsvariablen

Wichtige Einstellungen in `.env`:

```env
# Datenbank
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mandari

# Redis
REDIS_URL=redis://localhost:6379/0

# Elasticsearch (optional)
ELASTICSEARCH_URL=http://localhost:9200

# KI: Anbieter und Schlüssel im Admin (KI-Einstellungen); hier nur die Positivliste erlaubter Hosts
# (leer ist nichts erlaubt, die KI bleibt aus)
KI_ERLAUBTE_HOSTS=

# Django
SECRET_KEY=your-secret-key
DEBUG=True
```

## Projektstruktur

```
mandari/
├── mandari/           # Django Projekt-Settings
├── insight_core/      # Hauptanwendung (Views, Templates)
├── insight_sync/      # OParl Synchronisation
├── insight_search/    # Elasticsearch Integration
├── insight_ai/        # KI-Features (OpenAI-kompatibel, zentrale KI-Konfiguration)
├── templates/         # Django Templates
└── static/           # Statische Dateien
```

## OParl-Synchronisation

Die Synchronisation übernimmt der Ingestor (`ingestor/`, eigener Container). Quellen werden im
Admin angelegt; manuelle Läufe im Ingestor-Container:

```bash
# OParl-Quelle registrieren
python -m src.main add-source https://oparl.example.com/api

# Registrierte Quellen mit Status, letztem Sync und Zahl der Kommunen anzeigen
python -m src.main list-sources

# Kommunen (Bodies) aller Quellen anzeigen
python -m src.main list-bodies

# Alle aktiven Quellen synchronisieren
python -m src.main sync --all
```

Neue Quellen sind inaktiv: `sync --all` und der Daemon gleichen sie erst ab, wenn sie im Admin
aktiviert sind. Neu entdeckte Kommunen sind nicht gelistet und erscheinen im Bürgerportal erst,
wenn sie im Admin gelistet sind (Issue #555). Ausnahme ist die eigene OParl-Schnittstelle eines
Session-Mandanten, der die Veröffentlichung im Bürgerportal eingeschaltet hat: Ihre Quelle ist
aktiv und ihre Kommune gleich gelistet.

## Lizenz

AGPL-3.0-or-later – siehe [LICENSE](../LICENSE)
