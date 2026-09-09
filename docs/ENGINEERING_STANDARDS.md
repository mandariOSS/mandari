# Engineering-Standards

Stand: September 2026. Diese Regeln gelten für neuen und für angefassten Code. Bestandscode wird
schrittweise nachgezogen; die Ratchet-Gates in der CI stellen sicher, dass Kennzahlen nur besser werden.
Architekturentscheidungen werden als ADR unter [`docs/adr/`](adr/) festgehalten.

## 1. Schichten (Backend)

- **Views orchestrieren, Services entscheiden, Selectors lesen, Models validieren.** Ab dem zweiten
  Schreibzugriff oder dem dritten Query gehört Logik in `services.py` bzw. `selectors.py` der App.
- Jede Service-Funktion, die mehr als ein Objekt schreibt, läuft in `transaction.atomic`.
- Keine Query in `get_context_data` ohne `select_related`/`prefetch_related`; kein ORM-Zugriff in Templates.
- Signals nur für lose Kopplung zwischen Apps (Indexierung, Benachrichtigung), nie für Kernlogik.
- Django und Ingestor schreiben dieselben Tabellen: Schemaänderungen an `insight_core` werden im selben
  PR im Ingestor nachgezogen (`ingestor/src/storage/models.py`) und im Smoke-Test abgesichert.

## 2. Typisierung und Stil

- Neuer Code: vollständige Parameter- und Rückgabe-Annotationen, Docstrings auf Deutsch für Fachlogik.
- Code-Identifier Englisch, Fachbegriffe in `verbose_name`, Meldungen und Kommentaren Deutsch.
- `ruff check` und `ruff format` sind blockierend; Regelsatz wird schrittweise erweitert (Ziel:
  `E,F,I,N,W,UP,B,S,DJ,SIM,C4,RET,ARG`). `mypy --strict` mit `django-stubs` als wachsendes Gate mit
  Allowlist, die nur schrumpfen darf.
- Keine `# noqa` ohne Begründung im selben Kommentar.

## 3. Tests

- Testpyramide: Unit-Tests für Services und Selectors, Integrationstests für Views mit `pytest-django`,
  Ende-zu-Ende nur für Kernpfade (Playwright).
- Jeder neue Endpunkt bringt einen Berechtigungs- und einen Mandanten-Isolationstest mit.
- Smoke-Skripte unter `scripts/smoke_*.py` sind Bestandsschutz und werden bei Berührung in Testmodule
  unter `apps/<app>/tests/` überführt.
- Coverage wird gemessen und als Gate mit steigender Schwelle geführt.

## 4. Sicherheit

- Alle Sicherheits-Header und Cookie-Flags werden in Django gesetzt, nicht nur im Reverse Proxy.
  `manage.py check --deploy --fail-level WARNING` ist ein blockierendes CI-Gate; Ausnahmen stehen mit
  Begründung in `SILENCED_SYSTEM_CHECKS`.
- Content-Security-Policy: derzeit Report-Only mit Nonce für Skripte. Neue Templates enthalten keine
  Inline-Skripte oder -Styles; Daten an JavaScript ausschließlich per `json_script` oder `data-`-Attribute.
- `|safe` und `mark_safe` nur mit Begründungskommentar; JSON niemals per `|safe`.
- Keine Secrets im Repo; Abhängigkeiten über Dependabot aktualisiert, Sicherheitsupdates innerhalb von
  sieben Tagen nach Veröffentlichung.

## 5. Frontend (Templates, CSS, JavaScript)

- Templates sind Struktur, nicht Programm: höchstens 300 Zeilen, kein `<script>`/`<style>` außerhalb der
  Allowlist (Layouts, E-Mails, PDF, PWA). Das Skript `scripts/check_frontend_ratchet.py` misst
  Inline-Skripte, Inline-Styles, `on*=`-Handler, `style=`-Attribute und Templates über 300 Zeilen; die
  Werte dürfen nur sinken.
- Was fünfmal identisch vorkommt, wird Komponente. Bibliothek: `templates/cotton/` (django-cotton) mit
  `ui/` (button, card, badge, alert, empty-state, modal, icon, th), `form/` (field, password, checkbox,
  errors) und `layout/` (page-header). Aufruf als Tag: `<c-ui.button variant="secondary" icon="plus">`,
  gebundene Django-Felder per `:field="form.email"`. Jede Komponente dokumentiert ihre Parameter im
  Kopfkommentar; die Vorschau liegt unter `/dev/ui/` (nur `DEBUG`) und wird von
  `apps/common/tests/test_components.py` mitgerendert. Neue Komponente = Vorschau-Eintrag + Test.
  Fragmente für HTMX per Django-6-Template-Partials in derselben Datei.
- JavaScript lebt in `frontend/` (TypeScript, gebündelt), Alpine-Komponenten werden mit `Alpine.data()`
  registriert und im Template nur referenziert. Server-Daten kommen per `json_script`.
- Styles kommen aus Tailwind-Utilities und `frontend/css`; Design-Tokens (Farben, Radien, Schatten,
  Typografie) sind zentral definiert.
- Barrierefreiheit ist Teil jeder Komponente: Tastaturbedienung, Fokus sichtbar, ARIA, Zielgröße
  mindestens 24 Pixel, Kontraste nach WCAG 2.2 AA.

## 6. API

- Neue Endpunkte mit generiertem OpenAPI-Schema (django-ninja), versioniert unter `/api/v1/`,
  Fehler nach RFC 9457. Bestehende `JsonResponse`-Views werden beim Anfassen migriert.
- Öffentliche Schnittstellen (OParl, Fraktions-API, Session-API) folgen der Release- und
  Deprecation-Politik: inkompatible Änderungen nur in neuer Version mit Ankündigungsfrist.

## 7. Betrieb und Beobachtbarkeit

- Strukturierte Logs mit Request-Kennung, kein `DEBUG`-Logging in Produktion.
- Hintergrundarbeit über Django Tasks oder Management-Kommandos, immer idempotent.
- Migrationen additiv; große Apps werden bei Gelegenheit gesquasht.

## 8. Dokumentation und Commits

- Conventional Commits auf Deutsch (`feat(session): …`, `fix(insight): …`), keine Signaturen von
  Werkzeugen oder Assistenten.
- Architekturentscheidungen als ADR (MADR-Format) unter `docs/adr/`.
- Öffentliche Dokumentation unter docs.mandari.de; Implementierungsnotizen unter `docs/`.
- Keine Preise, Konditionen, Zugangsdaten oder Infrastrukturdetails im Repo, in Issues oder Commits.
