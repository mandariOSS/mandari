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

### Frontend (Vite)

```bash
cd mandari
npm ci
npm run build          # Tailwind-CSS + Vite-Bundles nach static/dist/ (einmalig oder vor DEBUG=False)
npm run watch          # Entwicklung: Tailwind-Watch + Vite-Dev-Server mit HMR
```

Mit laufendem Dev-Server `DJANGO_VITE_DEV_MODE=1` setzen, damit Django die Module vom Vite-Server lädt.
Ohne Dev-Server reicht `npm run build`; Django nutzt dann das Manifest.

```bash
npm run typecheck      # tsc --noEmit
npm run lint           # Biome (Lint + Format)
npm run lint:fix
```

### Tests ausführen

```bash
cd mandari
uv run pytest
```

E-Mail-Templates werden in `apps/common/tests/test_emails.py` gegen Snapshots
(`apps/common/tests/snapshots/emails/*.html|*.txt`) geprüft. Nach einer gewollten Änderung an einer Mail
oder am Basis-Layout die Snapshots neu schreiben und mit committen:

```bash
cd mandari
UPDATE_SNAPSHOTS=1 uv run pytest apps/common/tests/test_emails.py
```

### End-to-End-Tests (Playwright)

```bash
cd mandari
pip install pytest-playwright && python -m playwright install chromium
npm run build                         # gebaute Assets, die der Live-Server ausliefert
MANDARI_E2E=1 pytest tests_e2e -q     # Kernpfade, axe-core, Screenshots unter tests_e2e/screenshots/
```

Ohne `MANDARI_E2E=1` werden die E2E-Tests übersprungen. Im CI laufen sie im Job „E2E“; die
Screenshots (hell/dunkel, Komponentenvorschau, Kernpfade) liegen dort als Artefakt.

### Coverage

Die CI verlangt mindestens 38 % Zeilenabdeckung (`--cov-fail-under`, Stand 09/2026: 40 %). Die
Schwelle wird mit wachsender Testbasis angehoben, nie gesenkt.

### Code-Style

Verbindlich ist [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md). Werkzeuge:

- **Ruff** für Linting und Formatierung (Python)
- **djlint** für Django-Templates
- **mypy** (strict) für neuen und angefassten Python-Code
- **pre-commit** führt alles vor jedem Commit aus

```bash
# einmalig
pip install pre-commit && pre-commit install

# manuell
cd mandari
ruff check . && ruff format --check .
djlint templates --lint
python ../scripts/check_frontend_ratchet.py
python ../scripts/mypy_allowlist.py
```

### UI-Komponenten

Wiederkehrendes Markup kommt aus der Komponentenbibliothek `mandari/templates/cotton/` (django-cotton).
Die Vorschau aller Komponenten läuft im Entwicklungsmodus unter `http://localhost:8000/dev/ui/`.
Neue Komponenten bekommen einen Kopfkommentar mit den Parametern, einen Eintrag in der Vorschau und
einen Test in `apps/common/tests/test_components.py`.

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
