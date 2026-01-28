# Mandari Insight

**Kommunalpolitische Transparenz mit KI-Unterstützung**

Mandari Insight ist eine Open-Source-Plattform für die Darstellung und Analyse von Ratsinformationssystemen (RIS) auf Basis des OParl-Standards.

## Technologie-Stack

- **Django 5.2+** - Web Framework mit Server-Side Rendering
- **PostgreSQL 15+** - Datenbank
- **Meilisearch** - Volltextsuche
- **Redis** - Caching und Sessions
- **Groq API** - KI-Features (Zusammenfassungen, Chatbot)
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
- Meilisearch (optional)

### Setup

```bash
# Repository klonen
cd mandari

# Virtual Environment erstellen
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows

# Dependencies installieren
pip install -e ".[dev]"

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

# Meilisearch (optional)
MEILISEARCH_URL=http://localhost:7700
MEILISEARCH_KEY=masterKey

# Groq API (für KI-Features)
GROQ_API_KEY=your-api-key

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
├── insight_search/    # Meilisearch Integration
├── insight_ai/        # KI-Features (Groq)
├── templates/         # Django Templates
└── static/           # Statische Dateien
```

## OParl-Synchronisation

```bash
# OParl-Quelle hinzufügen (via Admin oder Command)
python manage.py sync_oparl --source https://oparl.example.com/api --create

# Daten synchronisieren
python manage.py sync_oparl --all
```

## Lizenz

AGPL-3.0 - siehe [LICENSE](../LICENSE)
