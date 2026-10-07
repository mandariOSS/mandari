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

Wie wir Arbeit planen und verfolgen (Epics, Meilensteine, Status, Entscheidungen), steht in [docs/PROJEKTSTEUERUNG.md](docs/PROJEKTSTEUERUNG.md).

Bei größeren Änderungen lohnt sich ein Issue vorab — das erspart Arbeit, die am Ende nicht
zum Projekt passt.

## Entwicklungsumgebung

Voraussetzungen: Python 3.12 oder neuer, Node.js 22 oder neuer (CI und Image nutzen Node.js 26),
Docker (für PostgreSQL, Redis und Elasticsearch), [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari

# Datenbank, Cache und Suche starten
docker compose up -d postgres redis elasticsearch

cd mandari
cp ../.env.example .env          # SECRET_KEY und ENCRYPTION_MASTER_KEY eintragen
uv sync --extra dev              # Python-Abhängigkeiten aus uv.lock, mit Test- und Lint-Werkzeugen
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

### Python-Abhängigkeiten ändern

Die direkten Abhängigkeiten stehen in `pyproject.toml` (Django-Anwendung: `mandari/`, Ingestor:
`ingestor/`), die gesperrten Versionen in der `uv.lock` daneben. Image, CI, pip-audit und SBOM
lesen ausschließlich die Lock-Datei.

```bash
cd mandari                       # bzw. ingestor
# Abhängigkeit in pyproject.toml eintragen oder Grenze ändern, dann:
uv lock                          # nur das Nötige ändert sich, alles andere bleibt gesperrt
uv lock --upgrade-package django # eine gesperrte Version gezielt anheben
uv sync --extra dev              # lokale Umgebung nachziehen
```

`pyproject.toml` und `uv.lock` gehören in denselben Commit; die CI prüft mit `uv lock --check`,
dass beide zusammenpassen. Dependabot pflegt beide Dateien selbst.

## Tests und Prüfungen

Alle Prüfungen laufen auch in der CI und müssen grün sein.

Im Pull Request startet die CI nur die Jobs, deren Bereich die Änderung berührt (Job „Geänderte
Bereiche“ in `.github/workflows/pr-check.yml`; eine reine Ingestor-Änderung braucht zum Beispiel
keine Django-Testsuite). Maßgeblich ist der Job **„CI-Ergebnis“**: Er ist grün, wenn jeder nötige Job
bestanden hat. Änderungen an `.github/` oder an Abhängigkeitsdateien, jeder Push auf `main`
sowie jeder Lauf in der Merge-Queue lassen alle Jobs laufen. Wer einen neuen Job anlegt,
trägt ihn unter `needs` von `ci-ergebnis` ein (ein Test prüft das).

Ein Push auf `dev` startet keine CI: Auf `dev` landet genau das Commit, das die Merge-Queue geprüft
hat. Den Volllauf auf `dev` startet jede Nacht `.github/workflows/nachtlauf.yml`.

Die Django-Testsuite läuft in drei parallelen Teilen (Job „Test“, Issue #935):

- **Aufteilung:** `--teil N/3` verteilt ganze Testdateien nach den gemessenen Laufzeiten in
  `mandari/testdauern.json` (`apps/common/tests/testlauf.py`). Neue Dateien zählen mit der mittleren
  Dauer je Test. Wird ein Teil merklich länger als die anderen, `testdauern.json` durch die Datei aus
  dem Artefakt `test-ergebnis` eines aktuellen Laufs ersetzen.
- **Testdatenbank:** Jeder Job migriert einmal eine Vorlage (`scripts/testdb_vorlage.py`); jeder
  xdist-Worker bekommt eine Kopie (`CREATE DATABASE … TEMPLATE`, Variable `MANDARI_TEST_DB_VORLAGE`)
  statt alle Migrationen selbst abzuspielen.
- **Migrationstests:** Tests, die Migrationen zurück- und wieder vorspielen (`….migrate(...)` oder
  `call_command("migrate", ...)` im Test), bekommen automatisch das Kennzeichen `migrationen` und
  laufen im eigenen Job „Migrationstests“: im Pull Request und in der Merge-Queue nur, wenn
  Migrationen oder Migrationstests geändert sind, sonst bei jedem Volllauf (Push auf `main`, Nacht-
  und Wochenlauf, manueller Start). Migriert ein Test über eine Hilfsfunktion, erkennt die Automatik
  ihn nicht, und er scheitert mit einem Hinweis; dann `@pytest.mark.migrationen` setzen.
- **Ergebnis:** Der Job „Test-Ergebnis“ führt die Coverage aller Teile zusammen, prüft die
  Coverage-Grenze und belegt, dass jeder gesammelte Test genau einmal lief (Tabelle „Tests je Teil“
  in der Zusammenfassung des Laufs).

Zwei Grenzen des Filters: Die E2E-Tests laufen im Pull Request nur bei Templates, Frontend, Settings,
Anmeldung, Editor und den Views der Seiten, die sie aufrufen. Ändert ein PR etwa einen Service, der
den Kontext einer solchen Seite liefert, fällt ein Fehler im Browser erst in der Merge-Queue auf.
Wer eine E2E-Seite mittelbar ändert, startet den Lauf deshalb besser von Hand (Actions → CI →
„Run workflow“ auf dem eigenen Branch, das startet alle Jobs). Und sobald CodeQL im Workflow statt
im Default-Setup läuft, analysiert es je PR nur die betroffenen Sprachen; Code Scanning weist dann
womöglich darauf hin, dass Analysen des Basiszweigs (etwa `/language:actions`) fehlen. Das ist
erwartet und blockiert nichts.

```bash
cd mandari
uv run pytest                                    # über 10.000 Tests, auch die Migrationstests
uv run pytest -n auto -m "not migrationen"       # wie ein Teil der CI: parallel, ohne Migrationstests
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

Die Kollaborationstests (`tests_e2e/test_editor_kollaboration.py`) starten einen eigenen
ASGI-Testserver (Daphne im Thread, In-Memory-Channel-Layer, kein Redis) und öffnen zwei
Browserkontexte. Sie brauchen kein zusätzliches Setup, dauern aber je rund eine Minute.

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
lint-imports                                     # Schichtregeln (pip install import-linter==2.15)

cd ..
python scripts/check_import_linter_ratchet.py    # Schichtverträge nur strenger, Ausnahmen nur weniger
python scripts/mypy_allowlist.py                 # Typen (strict, schrumpfende Ausnahmeliste)
python scripts/check_frontend_ratchet.py         # Inline-Code und Template-Größe
python scripts/check_view_orm_ratio.py           # Datenbankzugriffe in Views
python scripts/check_schema_contract.py          # Anwendung gegen Ingestor-Schema
```

Am bequemsten übernimmt das pre-commit:

```bash
pip install pre-commit && pre-commit install
```

Die Coverage-Schwelle der CI liegt bei 38 Prozent; erreicht sind rund 87 Prozent (Stand 09/2026).
Sie wird mit wachsender Testbasis angehoben, nie gesenkt.

**Verweise in der Dokumentation** prüft der Job „Verweise (Link-Prüfung)“ mit
[lychee](https://github.com/lycheeverse/lychee): bei jeder Änderung an einer Markdown-Datei die
internen Verweise und Anker aller Markdown-Dateien, im Pull Request und im Wochenlauf zusätzlich die
externen Verweise der Dateien des öffentlichen Auftritts (README, diese Datei, `SECURITY.md`,
Issue- und PR-Vorlagen und weitere). Einstellungen und Ausnahmen stehen in `lychee.toml`. Lokal:

```bash
lychee --offline --include-fragments '**/*.md'   # interne Verweise und Anker, ohne Netz
lychee README.md CONTRIBUTING.md SECURITY.md     # externe Verweise
```

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

Gemergt wird über die **Merge-Queue** von `dev`, nicht direkt. Wer mergen darf, reiht einen
Pull Request mit

```bash
gh pr merge <nummer> --squash --auto
```

ein; er kommt in die Queue, sobald seine Prüfungen grün sind. Die Queue setzt ihn auf den
aktuellen Stand von `dev` und alle Pull Requests vor ihm, prüft diesen kombinierten Stand mit
allen Jobs der CI und merged erst, wenn „CI-Ergebnis“ grün ist. Scheitert die Prüfung, nimmt
sie den Pull Request wieder heraus; er bleibt offen. So landet auf `dev` nur, was zusammen mit
allem davor geprüft ist. Nach dem Merge holt sich die Staging-Umgebung den neuen Stand selbst
(siehe [DEPLOYMENT.md](DEPLOYMENT.md#staging-als-prüfstand)). An der Queue vorbei mergen
nur Administratoren im Notfall.

Entwicklung findet auf `dev` statt; `main` ist der Produktionsstand.

## Lizenz und Urheberrecht

mandari steht unter [AGPL-3.0-or-later](LICENSE).

Beiträge Dritter nehmen wir erst an, nachdem eine Beitragsvereinbarung (Contributor License
Agreement) geschlossen wurde. Die Vereinbarung wird derzeit finalisiert. Bis sie vorliegt: bitte
vor einem Pull Request ein Issue öffnen, damit wir das Vorgehen abstimmen können. Beiträge bleiben
in jedem Fall unter der AGPL frei verfügbar.

Für Organisationen, deren Beschäftigte beitragen, gibt es eine eigene Fassung.

Im Pull Request prüft der Check „Beitragsvereinbarung“, ob für alle Beteiligten eine Zustimmung
vorliegt; Projektkonten und Bots wie Dependabot sind ausgenommen. Stand, Ablauf und die dabei
verarbeiteten Daten stehen in [docs/cla/README.md](docs/cla/README.md).

Neue Dateien brauchen einen SPDX-Kopf, damit das Repository
[REUSE](https://reuse.software)-konform bleibt:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Dateien ohne Kopf (Bilder, Daten) werden in `REUSE.toml` zugeordnet. Prüfen mit
`reuse lint` (`pip install reuse`); in der CI läuft die Prüfung im Workflow „REUSE“.

### Urheberangaben

Urheber von mandari ist Sven Konopka. Die Urheberangabe steht an einer Stelle, in `REUSE.toml`, und
gilt von dort für jede Datei des Projekts; ausgenommen sind nur die dort eigens zugeordneten
Fremdbibliotheken. Dateiköpfe tragen deshalb nur die Lizenzkennung. Abweichende Sammelangaben
(etwa „Copyright (C) … Contributors“) gehören weder in Dateiköpfe noch in Paketmetadaten wie
`pyproject.toml`; ein Test in `apps/common/tests/test_lizenzangaben.py` prüft das.

Beitragende, deren Beiträge übernommen werden, nennen wir künftig zusätzlich zur zentralen Angabe,
nicht an ihrer Stelle:

- im Kopf jeder Datei, die sie wesentlich mitgestaltet haben, mit einer eigenen Zeile über der
  Lizenzkennung. REUSE führt diese Zeile mit der Angabe aus `REUSE.toml` zusammen.

  ```python
  # SPDX-FileCopyrightText: 2027 Erika Mustermann
  # SPDX-License-Identifier: AGPL-3.0-or-later
  ```

- über die Commits in der Liste der
  [Mitwirkenden](https://github.com/mandariOSS/mandari/graphs/contributors), auf die auch die
  README verweist.

Bitte keine Zeile für reine Formatierungen, Umbenennungen oder automatisch erzeugte Änderungen.

## Fragen

[Diskussionen](https://github.com/mandariOSS/mandari/discussions) für alles Allgemeine,
[Issues](https://github.com/mandariOSS/mandari/issues) für Fehler und Vorschläge.
