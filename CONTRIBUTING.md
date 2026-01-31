# Mitwirken bei Mandari

Danke, dass du zu Mandari beitragen möchtest! Dieses Dokument erklärt, wie du helfen kannst.

## Code of Conduct

Mit deiner Teilnahme an diesem Projekt stimmst du zu, unseren [Code of Conduct](CODE_OF_CONDUCT.md) einzuhalten.

## Wie kann ich helfen?

### Bug melden

1. Prüfe, ob der Bug bereits als [Issue](https://github.com/mandariOSS/mandari/issues) gemeldet wurde
2. Erstelle ein neues Issue mit dem **Bug Report** Template
3. Beschreibe das Problem so genau wie möglich
4. Füge Schritte zur Reproduktion hinzu

### Feature vorschlagen

1. Prüfe die [Discussions](https://github.com/mandariOSS/mandari/discussions), ob die Idee schon diskutiert wird
2. Erstelle ein Issue mit dem **Feature Request** Template
3. Beschreibe den Anwendungsfall und warum das Feature nützlich wäre

### Code beitragen

1. **Fork** das Repository
2. Erstelle einen **Feature Branch**: `git checkout -b feature/mein-feature`
3. **Committe** deine Änderungen: `git commit -m 'feat: Beschreibung'`
4. **Push** zum Branch: `git push origin feature/mein-feature`
5. Erstelle einen **Pull Request**

## Entwicklungsumgebung

### Voraussetzungen

- Python 3.12+
- Docker & Docker Compose
- [uv](https://github.com/astral-sh/uv) (Python Package Manager)

### Setup

```bash
# Repository klonen
git clone https://github.com/mandariOSS/mandari.git
cd mandari

# Infrastruktur starten
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d

# Backend setup
cd mandari
cp .env.example .env
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

### Tests ausführen

```bash
cd mandari
uv run pytest
```

### Code-Style

Wir nutzen:
- **Ruff** für Linting
- **Black** für Formatierung
- **isort** für Import-Sortierung

```bash
# Formatierung prüfen
ruff check .
black --check .
isort --check .

# Automatisch formatieren
black .
isort .
```

## Commit-Konventionen

Wir folgen [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <beschreibung>

[optionaler body]
```

**Types:**
- `feat`: Neues Feature
- `fix`: Bugfix
- `docs`: Dokumentation
- `style`: Formatierung (kein Code-Änderung)
- `refactor`: Code-Umstrukturierung
- `test`: Tests hinzufügen/ändern
- `chore`: Build, Dependencies, etc.

**Beispiele:**
```
feat: Volltextsuche für Vorlagen hinzufügen
fix: Login-Fehler bei 2FA beheben
docs: Installation-Guide aktualisieren
```

## Pull Request Prozess

1. Stelle sicher, dass alle Tests passieren
2. Aktualisiere die Dokumentation wenn nötig
3. Der PR wird von einem Maintainer reviewed
4. Nach Approval wird der PR gemergt

## Fragen?

- [GitHub Discussions](https://github.com/mandariOSS/mandari/discussions) für allgemeine Fragen
- [Issues](https://github.com/mandariOSS/mandari/issues) für Bugs und Feature Requests

---

Danke für deinen Beitrag! 🎉
