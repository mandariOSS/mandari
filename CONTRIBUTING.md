# Mitwirken bei mandari

Danke für das Interesse. Beiträge sind willkommen — Fehlermeldungen, Dokumentation,
Übersetzungen und Code gleichermaßen.

Mit der Teilnahme gilt der [Verhaltenskodex](CODE_OF_CONDUCT.md).

## Wo anfangen

| Anliegen | Weg |
|----------|-----|
| Fehler gefunden | Zuerst die [Issues](https://github.com/mandariOSS/mandari/issues) durchsehen, dann ein neues mit der Vorlage „Bug Report“ anlegen |
| Funktion vorschlagen | Erst in den [Diskussionen](https://github.com/mandariOSS/mandari/discussions) ansprechen, dann Issue mit der Vorlage „Feature Request“ |
| Sicherheitslücke | **Nicht** öffentlich: siehe [SECURITY.md](SECURITY.md) |
| Frage | [Diskussionen](https://github.com/mandariOSS/mandari/discussions) |
| Erster Beitrag | Issues mit der Markierung [`good first issue`](https://github.com/mandariOSS/mandari/labels/good%20first%20issue) |

Bei größeren Änderungen lohnt sich ein Issue vorab — das erspart Arbeit, die am Ende nicht
zum Projekt passt.

## Entwicklungsumgebung

Voraussetzungen: Python 3.12 oder neuer, Node.js 20, Docker (für PostgreSQL, Redis und
Elasticsearch), [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari

# Datenbank, Cache und Suche starten
docker compose up -d postgres redis elasticsearch

cd mandari
cp ../.env.example .env          # SECRET_KEY und ENCRYPTION_MASTER_KEY eintragen
uv sync                          # Python-Abhängigkeiten
npm ci && npm run build          # Frontend bauen

uv run python manage.py migrate
uv run python manage.py setup_roles
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Die Anwendung läuft dann unter <http://localhost:8000>.

Für die Frontend-Entwicklung mit Hot Reload:

```bash
npm run watch                    # Tailwind-Watch und Vite-Dev-Server
DJANGO_VITE_DEV_MODE=1 uv run python manage.py runserver
```

Ohne laufenden Dev-Server genügt `npm run build`; Django lädt die Dateien dann über das
Vite-Manifest.

### Vollständiger Stack

Wer die Installation als Ganzes braucht (mit Caddy, Website und Ingestor), nutzt den
Installer:

```bash
COMPOSE_PROJECT_NAME=mandari-dev ./install.sh
```

Der Projektname trennt diese Installation von anderen auf demselben Rechner.

## Tests und Prüfungen

Alle Prüfungen laufen auch in der CI und müssen grün sein.

```bash
cd mandari
uv run pytest                                    # rund 1.500 Tests, etwa 90 Sekunden
uv run pytest apps/work/tasks -q                 # einzelne App
```

**End-to-End im Browser** (Playwright mit axe-core für Barrierefreiheit):

```bash
pip install pytest-playwright && python -m playwright install chromium
npm run build
MANDARI_E2E=1 pytest tests_e2e -q
```

Ohne `MANDARI_E2E=1` werden diese Tests übersprungen. Screenshots landen unter
`tests_e2e/screenshots/`, in der CI als Artefakt.

**E-Mail-Vorlagen** werden gegen Snapshots geprüft. Nach einer gewollten Änderung:

```bash
UPDATE_SNAPSHOTS=1 uv run pytest apps/common/tests/test_emails.py
```

**Statische Prüfungen:**

```bash
cd mandari
ruff check . && ruff format --check .            # Python
djlint templates --lint                          # Django-Templates
npm run typecheck && npm run lint                # TypeScript und Biome

cd ..
python scripts/mypy_allowlist.py                 # Typen (strict, schrumpfende Ausnahmeliste)
python scripts/check_frontend_ratchet.py         # Inline-Code und Template-Größe
python scripts/check_view_orm_ratio.py           # Datenbankzugriffe in Views
python scripts/check_schema_contract.py          # Anwendung gegen Ingestor-Schema
```

Am bequemsten übernimmt das pre-commit:

```bash
pip install pre-commit && pre-commit install
```

Die Coverage-Schwelle liegt bei 38 Prozent (Stand: 40 Prozent erreicht). Sie wird mit
wachsender Testbasis angehoben, nie gesenkt.

## Konventionen

Verbindlich ist [`docs/ENGINEERING_STANDARDS.md`](docs/ENGINEERING_STANDARDS.md);
Architekturentscheidungen stehen unter [`docs/adr/`](docs/adr/). Das Wichtigste:

**Python.** Neuer und angefasster Code ist typisiert. Datenzugriff gehört in `selectors.py`,
schreibende Fachlogik in `services.py` — Views bleiben dünn. Jede Abfrage im
Arbeitsbereich filtert nach Organisation. Schreibende Pfade über mehrere Tabellen laufen in
`transaction.atomic`.

**Templates.** Struktur, kein Programm: höchstens 300 Zeilen, kein `<script>` oder `<style>`
in Seiten-Templates. Wiederkehrendes Markup kommt aus der Komponentenbibliothek
`templates/cotton/`; die Vorschau läuft unter `/dev/ui/`. Neue Komponenten brauchen einen
Kopfkommentar mit den Parametern, einen Eintrag in der Vorschau und einen Test in
`apps/common/tests/test_components.py`.

**JavaScript.** Liegt als TypeScript unter `frontend/` und wird mit Vite gebaut.
Alpine-Komponenten werden mit `Alpine.data()` registriert und im Template nur per
`x-data="name"` referenziert. Server-Daten kommen per `json_script` zum Client, nicht als
interpolierter String.

**Barrierefreiheit.** Tastaturbedienung, sichtbarer Fokus, ARIA und Zielgrößen gehören zu
jeder Komponente. Die E2E-Tests prüfen das mit axe-core.

**Deutsch.** Kommentare, Commit-Nachrichten und Dokumentation auf Deutsch; Bezeichner im Code
auf Englisch.

## Commits und Pull Requests

Wir folgen [Conventional Commits](https://www.conventionalcommits.org/):

```
<typ>(<bereich>): <beschreibung>

<optionaler Fließtext>
```

Typen: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.

```
feat(session): Vorlagen als PDF exportieren
fix(work): Anmeldung bei aktiver Zwei-Faktor-Prüfung repariert
docs: Installationsanleitung für Kubernetes ergänzt
```

Für einen Pull Request:

1. Repository abspalten, Branch von `dev` anlegen (`git checkout -b feat/mein-thema`)
2. Änderungen mit Tests versehen
3. Alle Prüfungen lokal laufen lassen
4. Pull Request gegen `dev` öffnen und beschreiben, **was** sich ändert und **warum**
5. Bezug zum Issue herstellen (`Fixes #123`)

Entwicklung findet auf `dev` statt; `main` ist der Produktionsstand.

## Lizenz und Urheberrecht

Beiträge stehen unter [AGPL-3.0-or-later](LICENSE). Neue Dateien brauchen einen
SPDX-Kopf, damit das Repository [REUSE](https://reuse.software)-konform bleibt:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Dateien ohne Kopf (Bilder, Daten) werden in `REUSE.toml` zugeordnet. Prüfen mit
`reuse lint`.

## Fragen

[Diskussionen](https://github.com/mandariOSS/mandari/discussions) für alles Allgemeine,
[Issues](https://github.com/mandariOSS/mandari/issues) für Fehler und Vorschläge.
