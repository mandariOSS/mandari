# Engineering-Standards

Diese Regeln gelten für neuen und für angefassten Code. Bestandscode wird schrittweise nachgezogen;
die Ratchet-Gates in der CI stellen sicher, dass Kennzahlen nur besser werden. Architekturentscheidungen
werden als ADR unter [`docs/adr/`](adr/) festgehalten. Wie wir planen und ausliefern, steht in
[WEITERENTWICKLUNG.md](WEITERENTWICKLUNG.md).

## 1. Schichten (Backend)

- **Abhängigkeiten zeigen nur nach unten:** Fachmodule (`apps.session`, `apps.work`, `apps.minutes`)
  → Drehscheibe (`hub`, `insight_*`) → Plattform (`apps.events`, `apps.common`,
  `apps.accounts`, `apps.tenants`, `apps.provisioning`); Session, Work und Minutes kennen sich nicht
  ([ADR Schichtenmodell](adr/20260929-schichtenmodell.md)). `lint-imports` prüft das in der CI,
  Importe in Funktionen zählen mit, Tests sind ausgenommen. Die Ausnahmelisten in
  `mandari/pyproject.toml` dürfen nur kürzer werden, die Verträge selbst nur strenger
  (`scripts/check_import_linter_ratchet.py`, Baseline `scripts/import_linter_baseline.json`; nach
  einem Abbau oder einer Verschärfung `--update` im selben PR). Jede neue App muss einer Schicht
  zugeordnet werden, jedes neue Fachmodul zusätzlich dem Vertrag `module-unabhaengig`.
- **Notation im Schichtenvertrag:** Abweichend von der ADR (`|`) trennt `:` die Pakete einer Schicht.
  Mit `|` wären auch Pakete derselben Schicht gegenseitig verboten, etwa `apps.tenants` und
  `apps.accounts` in der Plattform. Die Unabhängigkeit der Fachmodule sichert stattdessen der
  eigene Vertrag `module-unabhaengig`.
- **RIS-Daten nur über die Lese-Fassade:** Fachmodule lesen Sitzungen, Tagesordnungspunkte, Vorlagen,
  Gremien, Personen und Dateien über `hub.ris.selectors`, nicht über `OParl*.objects`
  ([ADR Kanonisches Modell](adr/20260929-kanonisches-modell.md)). Fehlt eine Abfrage, kommt sie dort
  hinzu. `scripts/check_ris_access_ratchet.py` zählt die verbliebenen Direktzugriffe außerhalb von
  `hub` und `insight_core` je Datei (Baseline `scripts/ris_access_baseline.json`); sie dürfen nur
  sinken, nach einem Abbau `--update` im selben PR.
- **Ereignisse veröffentlicht nur der Eigentümer:** Jeder Datenbestand hat genau einen Eigentümer, nur
  er schreibt. `publish()` für einen Ereignistyp steht nur im Paket, das sein Vertrag als `x-owner`
  nennt ([ADR Ereignisverträge](adr/20260929-ereignisvertraege.md)). `ris.*` gehört der Drehscheibe
  (`hub.ris`, Übertragungen `ris.broadcast.*` in `hub.live`): Session meldet über die Abbildung in
  `hub/ris/`, nicht mit eigenem `publish()`, der Ingestor schreibt `ris.*` als Eingangsadapter direkt
  ins Journal. Die CI prüft das noch nicht; im Review darauf achten.
- **Befehle statt Direktaufruf:** Wer Daten eines anderen Moduls ändern will, schickt dem Eigentümer
  einen Befehl über `hub.commands` (`InProcessClient`, bei getrennten Installationen `HttpClient`) und
  bekommt eine Quittung oder ein Problem nach RFC 9457
  ([ADR Befehle](adr/20260929-befehle-synchron.md)). Kein Aufruf fremder Services, kein Schreiben in
  fremde Tabellen. Den Handler registriert nur das Paket aus `x-owner` (`command_handler`); sonst bricht
  der Start ab. Auch lesend greift ein Modul nicht auf fremde Tabellen zu, weder über deren Modelle
  noch per SQL; RIS-Daten liest es nur über `hub.ris.selectors` (siehe oben).
- **Jedes Ereignis und jeder Befehl hat ein Schema:** JSON Schema unter
  `hub/contracts/schemas/<typ>/v<n>.json` mit `x-owner`, `x-visibility` und Beispielen, im selben PR wie
  der erste Aufruf. Schemas ändern sich nur additiv, sonst entsteht eine neue Version.
  `scripts/check_event_contracts.py` prüft in der CI, dass jeder im Code genannte Typ ein Schema hat, dass
  Änderungen additiv sind und dass `docs/EREIGNISKATALOG.md` aktuell ist. In den Tests prüft `publish()`
  jedes Ereignis gegen das Register (`EVENTS_VALIDATE_CONTRACTS`).
- **Views orchestrieren, Services entscheiden, Selectors lesen, Models validieren.** Ab dem zweiten
  Schreibzugriff oder dem dritten Query gehört Logik in `services.py` bzw. `selectors.py` der App.
- Jede Service-Funktion, die mehr als ein Objekt schreibt, läuft in `transaction.atomic`.
- Keine Query in `get_context_data` ohne `select_related`/`prefetch_related`; kein ORM-Zugriff in Templates.
- Signals nur für lose Kopplung zwischen Apps (Indexierung, Benachrichtigung), nie für Kernlogik.
- Django und Ingestor schreiben dieselben Tabellen: Schemaänderungen an `insight_core` werden im selben
  PR im Ingestor nachgezogen (`ingestor/src/storage/models.py`) und im Smoke-Test abgesichert.

## 2. Typisierung und Stil

- Neuer Code: vollständige Parameter- und Rückgabe-Annotationen, Docstrings auf Deutsch für Fachlogik.
- Bezeichner: Bestehende Modelle, Felder und Schnittstellen bleiben englisch. Neuer Code darf deutsche
  Bezeichner verwenden, wo die Fachsprache deutsch ist (Verwaltungsbefehle wie `dokumentkette`,
  Dienste wie `pruefe_*`); innerhalb eines Moduls einheitlich. `verbose_name`, Meldungen und Kommentare
  sind deutsch.
- `ruff check` und `ruff format` sind blockierend; Regelsatz wird schrittweise erweitert (Ziel:
  `E,F,I,N,W,UP,B,S,DJ,SIM,C4,RET,ARG`). `mypy --strict` mit `django-stubs` als wachsendes Gate mit
  Allowlist, die nur schrumpfen darf.
- Keine `# noqa` ohne Begründung im selben Kommentar.
- **Toter Code darf nur weniger werden:** `scripts/check_dead_code_ratchet.py` lässt vulture über
  Anwendung, gemeinsame Pakete und Ingestor laufen und scheitert an jeder unbenutzten Funktion, Methode,
  Klasse oder Property, die nicht in `scripts/dead_code_baseline.json` steht. Was das Framework aufruft
  (Signale, Template-Tags, Management-Befehle, Admin, Methoden einer Basisklasse), steht mit Begründung in
  `scripts/dead_code_ratchet.toml`, nicht als `noqa` im Code; Namen aus Vorlagen zählen als Verwendung.
- **Doppelter Code darf nur weniger werden:** `scripts/check_duplicate_code_ratchet.py` zählt mit jscpd
  kopierte Blöcke ab 100 Token und 10 Zeilen in Python, Vorlagen, TypeScript und JavaScript (ohne Tests und
  Migrationen, Regeln in `scripts/duplicate_code_ratchet.toml`); Klone und doppelte Zeilen dürfen je
  Format nicht über `scripts/duplicate_code_baseline.json` steigen. Nach einem Abbau zieht `--update`
  die jeweilige Baseline im selben PR nach. Beide Berichte liegen als Artefakt `code-bericht` am CI-Lauf.

## 3. Tests

- Testpyramide: Unit-Tests für Services und Selectors, Integrationstests für Views mit `pytest-django`,
  Ende-zu-Ende nur für Kernpfade (Playwright).
- Jeder neue Endpunkt bringt einen Berechtigungs- und einen Mandanten-Isolationstest mit.
- Smoke-Skripte unter `scripts/smoke_*.py` sind Bestandsschutz und werden bei Berührung in Testmodule
  unter `apps/<app>/tests/` überführt.
- Coverage wird gemessen und als Gate mit steigender Schwelle geführt; die CI führt sie über alle Teile des
  Jobs „Test“ zusammen und belegt, dass jeder Test genau einmal lief (CONTRIBUTING.md, „Tests und Prüfungen“).
- Tests, die Migrationen zurück- und wieder vorspielen, tragen das Kennzeichen `migrationen` (automatisch
  erkannt) und laufen in einem eigenen CI-Job, wenn Migrationen oder diese Tests sich ändern, sowie nächtlich.

## 4. Sicherheit

- Alle Sicherheits-Header und Cookie-Flags werden in Django gesetzt, nicht nur im Reverse Proxy.
  `manage.py check --deploy --fail-level WARNING` ist ein blockierendes CI-Gate; Ausnahmen stehen mit
  Begründung in `SILENCED_SYSTEM_CHECKS`.
- Neue Templates enthalten keine Inline-Skripte oder -Styles; Daten an JavaScript ausschließlich per
  `json_script` oder `data-`-Attribute (Stand der Policy in 4.2).
- `|safe` und `mark_safe` nur mit Begründungskommentar; JSON niemals per `|safe`.
- Keine Secrets im Repo. Abhängigkeiten aktualisiert Dependabot; Sicherheitsupdates folgen den Fristen
  nach Schweregrad aus der [Release- und Support-Politik, Abschnitt 4](RELEASE_POLITIK.md#4-sicherheitskorrekturen-fristen).

### 4.1 Uploads

BSI IT-Grundschutz verlangt als Basis-Anforderung, Upload-Funktionen nach Dateigröße,
Dateityp und Speicherort einzuschränken (CON.10.A5, APP.3.1.A4). In mandari steht diese
Regel an genau einer Stelle: `apps/common/uploads.py`, Funktion `validate_upload`.
Jede Upload-Stelle ruft sie mit einem Profil und einer Höchstgröße auf (Issue #260).

| Profil | Endungen | Einsatz |
|---|---|---|
| `DOCUMENTS` | pdf, doc, docx, odt, rtf, txt, md, xls, xlsx, ods, csv, ppt, pptx, odp, png, jpg, jpeg, gif, webp, zip | Anlagen der Gremien- und Fraktionsarbeit |
| `IMAGES` | png, jpg, jpeg, gif, webp | Dateien, die eingebettet angezeigt werden (Logos, Profilbilder) |
| `PDF` | pdf | Briefköpfe, interner Import in Fraktionssitzungen |
| `IMPORTABLE_DOCUMENTS` | pdf, docx | Antrags-Import mit Textübernahme |
| `DATA` | csv, json, xml | Import-Schnittstellen; nur geparst (XML über defusedxml), nie gespeichert oder ausgeliefert |

`NEVER` sticht jedes Profil: HTML, SVG, XSL/XSLT, JavaScript, Serverskripte und
ausführbare Dateien werden nie angenommen. SVG fehlt auch im Bildprofil bewusst, weil
eine SVG-Datei Skript enthalten kann und vom Browser als aktives Dokument behandelt wird.
Gemessen wird die letzte Endung des Dateinamens; Pfadangaben im Namen ändern nichts.

| Bereich | Profil | Höchstgröße | Prüfende Stelle |
|---|---|---|---|
| Session: Anlagen zu Vorlagen und Sitzungen | `DOCUMENTS` | `file_service` | `apps/session/services/file_service.py` |
| Session: Anlagen über das Profil Einreichung | `DOCUMENTS`, nur Endungen der Antrags-Anhänge | `file_service` | `apps/session/api/einreichung.py` (`uploads_view`) |
| Work: Anlagen zu Anträgen | `DOCUMENTS` | 50 MB | `apps/work/motions/attachments.py` (`add_attachments`) |
| Work: Antrags-Import (Textübernahme) | `IMPORTABLE_DOCUMENTS` | 25 MB | `MotionImportView` |
| Work: Briefköpfe | `PDF` | 10 MB | `LetterheadCreateView`, `LetterheadEditView` |
| Work: Sitzungsvorbereitung | `DOCUMENTS` | 50 MB | `meetings/services.add_document_upload` |
| Work: Fraktionssitzungen | `DOCUMENTS` | 20 MB | `faction/views/panel.py`, `faction/views/session.py` |
| Work: interner Import in Fraktionssitzungen | `PDF` | 25 MB | `faction/views/internal_import.py` |
| Work: Aufgaben-Anhänge | `DOCUMENTS` | 20 MB | `TaskAttachmentForm.clean_file` |
| Work: Aufgaben-Import | `DATA` | 5 MB | `TaskFileImportView` |
| Work: Support-Anhänge | `DOCUMENTS` | 10 MB | `support/views.py` (ungeeignete Anlagen werden übergangen) |
| Work: Organisationslogo | `IMAGES` | 5 MB | `organization/services._pruefe_bild` |
| Work: Profilbild | `IMAGES` | 5 MB | `organization/services._pruefe_bild` |

**Auslieferung:** `serve_media` liefert alles außer eingebetteten Bildformaten mit
`Content-Disposition: attachment` und `X-Content-Type-Options: nosniff` aus. Ein
hochgeladenes Dokument wird also heruntergeladen, nicht im Browser gerendert; nur
Logos, Profilbilder und ähnliche Bilder werden eingebettet (`is_embeddable`).

Dateien unter `PROTECTED_MEDIA_PREFIXES` (`mandari/media.py`) liefert `serve_media` gar
nicht aus, auch nicht an angemeldete Personen: Session-Anlagen, Work-Anhänge
(`apps/work/files.py`), Datenexporte, Archivpakete des Protokolls und der Dokument-Cache.
Sie gehen nur über Download-Ansichten hinaus, die Mandant, Sichtbarkeit und Rechte prüfen.
Zeigen `OPARL_FILES_ROOT` oder `AUDIT_ARCHIVE_ROOT` in ein Verzeichnis unter `MEDIA_ROOT`,
gilt es ebenfalls als geschützt. Wer eine neue nichtöffentliche Ablage unter `MEDIA_ROOT`
einführt, trägt ihr Präfix dort ein.

Vorhandene Dateien unter `PUBLIC_MEDIA_PREFIXES` (Logos, Personenfotos) liefert schon
`PublicMediaMiddleware` vor Sitzung und Anmeldung aus: ohne Datenbankzugriff und ohne Cookie. Sonst
schrieb jedes Bild wegen `SESSION_SAVE_EVERY_REQUEST` die Sitzung zurück und brauchte eine
Verbindung aus dem Pool; eine Seite mit Dutzenden Fotos leerte ihn (Issue #667). Geschützte und
anmeldepflichtige Präfixe, fehlende Dateien und der Wartungsmodus nehmen weiter den regulären Weg.

**Gate:** `scripts/check_upload_validation.py` läuft im Lint-Job und meldet jedes Modul, das
`request.FILES` liest oder ein Datei-Formularfeld deklariert, ohne `validate_upload`
zu benutzen. Wer die Datei an eine prüfende Stelle weiterreicht, trägt das Modul mit
Begründung in `DELEGIERT` ein; die Liste darf nur schrumpfen.

### 4.2 Content-Security-Policy

- **Policy:** `SECURE_CSP_REPORT_ONLY` in `mandari/mandari/settings.py` (Django 6). Der Browser meldet
  Verstöße an `/csp-report/` (`apps/common/csp.py`, Logger `mandari.csp`, Zähler
  `mandari_csp_violations_total{directive}`), blockiert aber nichts. Erzwungen wird bislang nur die
  permissive Policy des Reverse Proxys (Issue #172). Die Betriebssicht steht in
  [MONITORING.md](MONITORING.md).
- **script-src:** `'self'`, Nonce, `'unsafe-eval'`. Der Nonce steht an allen Inline-Skripten
  (`nonce="{{ csp_nonce }}"`, Kontextprozessor `django.template.context_processors.csp`).
  `'unsafe-eval'` braucht Alpine (Ausdrücke in `x-data`, `@click`, `:class` …).
- **style-src:** `'self'`, `'unsafe-inline'`. Alle `<style>`-Blöcke in Templates, die ein Browser erhält
  (Layouts, Admin, Entwicklungsseiten, PWA und die verbleibenden Seiten), tragen ebenfalls den Nonce.
  Wirksam wird das erst, wenn `style-src` einen Nonce verlangt.
- **Ohne Nonce bleiben** E-Mail-Vorlagen (`emails/`, `work/notifications/email/`) und Vorlagen, die nur
  serverseitig zu PDF oder Exportdateien gerendert werden (`*/pdf/`, `work/motions/export/pdf_template.html`,
  `work/profile/export/`). Sie werden ohne Request gerendert und nie unter einer CSP ausgewertet.
- **Fehlerseite 500** (`errors/base_error.html`): wird absichtlich ohne Request-Kontext gerendert
  (`mandari/urls.py`, `handler_500`), der Nonce ist dort leer. Sobald `style-src` einen Nonce verlangt,
  braucht die Seite ihre Styles aus `styles.css` oder einen eigenen Nonce.
- **Inline-Handler:** keine (`on*=`, `hx-on`; Ratchet `on_handlers` = 0). Aktionen laufen über
  `data-*`-Attribute (`frontend/js/actions.ts`, `frontend/js/htmx-setup.ts`).
- **htmx:** `htmx.config.allowEval = false`, `selfRequestsOnly = true`; keine `hx-vars`, keine
  `hx-vals="js:…"`, keine Trigger-Filter mit `[…]`.
- **Verbleibende Inline-Stellen:** Die Zahlen führt `scripts/frontend_ratchet_baseline.json`, sie dürfen
  nur sinken. Inline-Skripte (alle mit Nonce) stehen noch im Admin-Dashboard (Chart.js), im Chat
  (`pages/chat.html`), in den beiden Kalendern (FullCalendar), im Teleprompter, beim Umsortieren der
  Tagesordnung in Session (`session/meetings/detail.html`) und beim Nachrichten-Polling im Support.
  Inline-Styles stehen in Layout-nahen Blöcken (`accounts/base_auth.html`, `errors/base_error.html`,
  `admin/_monitor_styles.html`), Kalender, Sitzungsvorbereitung, Teleprompter, den Editor-Styles
  (`work/motions/partials/_editor_styles.html`) sowie PDF- und Exportvorlagen (bleiben). Alle per HTTP
  ausgelieferten tragen den Nonce.

## 5. Frontend (Templates, CSS, JavaScript)

### 5.1 Regeln

- Templates sind Struktur, nicht Programm: höchstens 300 Zeilen, kein `<script>`/`<style>` außerhalb der
  Allowlist (Layouts, E-Mails, PDF, PWA). Das Skript `scripts/check_frontend_ratchet.py` misst
  Inline-Skripte, Inline-Styles, `on*=`-Handler, `style=`-Attribute und Templates über 300 Zeilen; die
  Werte dürfen nur sinken.
- Keine Inline-Handler (`onclick=`, `onsubmit=`, `onchange=`): Aktionen stehen als `data-*`-Attribute im
  Template (`data-confirm`, `data-autosubmit`, `data-href`, `data-action`, `data-post`, …), `frontend/js/actions.ts`
  hängt die delegierten Listener an. Voraussetzung für eine CSP ohne `unsafe-inline` (#172).
  Gleiches gilt für htmx: kein `hx-on`, keine `hx-vals="js:…"`, keine Trigger-Filter `[…]`; `allowEval` ist aus;
  Nachbearbeitung über `data-autosave`/`data-after-request` (`frontend/js/htmx-setup.ts`).
- Was fünfmal identisch vorkommt, wird Komponente. Bibliothek: `templates/cotton/` (django-cotton) mit
  `ui/` (button, card, badge, alert, empty-state, modal, tabs/tab/tab-panel, icon, th), `form/` (field,
  password, select, textarea, checkbox, errors) und `layout/` (page-header). Aufruf als Tag: `<c-ui.button variant="secondary" icon="plus">`,
  gebundene Django-Felder per `:field="form.email"`. Jede Komponente dokumentiert ihre Parameter im
  Kopfkommentar; die Vorschau liegt unter `/dev/ui/` (nur `DEBUG`) und wird von
  `apps/common/tests/test_components.py` mitgerendert. Neue Komponente = Vorschau-Eintrag + Test.
  Fragmente für HTMX per Django-6-Template-Partials in derselben Datei.
- JavaScript lebt in `frontend/` (TypeScript) und wird mit Vite gebaut; Layouts laden Assets über
  `{% vite_asset %}` (django-vite, Manifest in `static/dist/`). Einstiege (`vite.config.ts`):
  `frontend/js/main.ts` (alle Layouts: HTMX-Konfiguration, Alpine-Registry, Icon-Observer, Toasts,
  Bestätigungsdialog), `frontend/js/work.ts` (Komponenten des Work-Portals),
  `frontend/editor/index.ts` (nur Editor-Seiten) und `frontend/js/webauthn.ts` (Sicherheitsschlüssel).
  Alpine-Komponenten werden mit `Alpine.data()` registriert und im Template nur referenziert (5.2).
- Icons: `<i data-lucide="name">` genügt, ein MutationObserver ersetzt neue Platzhalter nach HTMX-Swaps
  und Alpine-Rendering. Kein `lucide.createIcons()` in Templates. Server-Toasts per
  `HX-Trigger: {"showToast": {"message": "…", "type": "success"}}`.
- `tsc --noEmit` und Biome (Lint + Format) laufen im CI; Frontend-Quellen sind LF-normalisiert.
- Styles kommen aus Tailwind-Utilities und `static/css/input.css` (gebaut nach `static/css/styles.css`);
  Design-Tokens (Farben, Radien, Schatten, Typografie) sind zentral definiert.
- Schema-Contract: Django-Modelle und Ingestor-Tabellen (`oparl_*`) müssen zusammenpassen;
  `scripts/check_schema_contract.py` prüft das im CI. Pflichtfelder, die der Ingestor nicht setzt,
  bekommen `db_default`. Vorgehen: `docs/adr/20260909-schema-contract-django-ingestor.md`.

### 5.2 Muster für Seiten ohne Inline-Code

- **Alpine-Komponente** in `frontend/alpine/<bereich>.ts` mit `defineComponent(() => ({ … }))`,
  registriert per `Alpine.data('name', …)` in `frontend/js/work.ts` (Work-Portal) bzw.
  `frontend/js/main.ts` (alle Layouts; Insight-Komponenten im Zweig `data-portal="insight"`).
  Im Template nur `x-data="name"`, ohne Klammern und ohne Argumente.
- **Startwerte:** Listen und Objekte per `{{ wert|json_script:"id" }}` (lesen mit
  `readJsonScript` aus `frontend/js/json-script.ts`), einzelne Werte als `data-*` am Element mit
  `x-data`. Nie Template-Variablen in JavaScript-Literale im Attribut schreiben
  (`x-data="farbe('{{ … }}')"`): Ein Wert mit Anführungszeichen bricht sonst aus dem String aus.
  Zahlen und Koordinaten unlokalisiert ausgeben (`{% load l10n %}` und `|unlocalize`), sonst kommt
  „51,96“ beim Client an.
- **`this.$el` nur in `init()`:** Ruft ein Ausdruck im Template eine Methode auf (`@click="speichern()"`),
  ist `$el` dort das auslösende Element, nicht die Wurzel der Komponente. Konfiguration deshalb in
  `init()` in Felder übernehmen, DOM-Referenzen in einer Closure der Fabrik halten
  (Beispiele: `permissionsManager`, `ticketForm`, `assignmentHandler`). `$refs` funktioniert überall,
  steht aber erst nach `init()` bereit (`$nextTick`).
- **Verhalten ohne Zustand** (URL-Kürzel, abhängige Auswahl, Textbausteine, Fehlerbericht) als
  `data-*`-Attribute mit delegierten Listenern: `frontend/js/form-behaviors.ts`; Bestätigen und Absenden
  per `data-action="confirm-submit"` (`frontend/js/actions.ts`).
- **Styles:** Tailwind-Klassen oder `static/css/input.css`. `:style` nur als Objekt binden
  (`:style="swatchStyle"` mit `{ backgroundColor: … }`): Alpine setzt Objekte über das CSSOM, Strings
  dagegen per `setAttribute('style', …)`, und das fällt unter `style-src-attr`. Zustände wie „ausgewählt“
  wenn möglich rein per CSS (`has-[:checked]:…`, `peer-checked:…`, siehe `work/profile/visibility.html`).
- **Keine Inline-Skripte in HTMX-Fragmenten:** Die Fragment-Antwort hat einen anderen Nonce als die Seite;
  unter einer erzwungenen Policy liefe das Skript nicht.
- **Start-Reihenfolge:** `main.ts` startet Alpine erst bei `DOMContentLoaded`; `work.ts` und das
  Editor-Bundle registrieren ihre Komponenten auf oberster Ebene davor. Editor-Instanzen (TipTap) bleiben
  außerhalb reaktiver Daten.
- **Karten:** Bürgerportal (`pages/map.html`, `insightKarte`) und Recherche in Work
  (`work/ris/partials/_karte.html`, `risKarte`) nutzen das gemeinsame Kartenmodul
  `frontend/js/vorgangskarte.ts` (Leaflet mit Kachel-Proxy, Punktgruppe, Abfrage je Ausschnitt, Popups
  aus DOM-Knoten) und dieselbe Abfrage der Punkte (`insight_core/services/karten_punkte.py`). Leaflet
  kommt als Vendor-Skript vor dem Modul.
- **Tests:** Jede ausgelagerte Komponente bekommt einen E2E-Test mit der Fixture `problems`
  (`tests_e2e/conftest.py`: Alpine-Fehler, Ausnahmen, Serverfehler) und prüft die typische Bedienung.

### 5.3 E-Mails

Jede HTML-Mail erweitert `templates/emails/base_email.html` (tabellenbasiertes 600-px-Layout,
Blöcke `subject`, `preheader`, `content`, `footer`). Nur das Basis-Layout hat einen `<style>`-Block;
Fach-Mails nutzen ausschließlich dessen Klassen (`.h1`, `.card`, `.btn`, `.muted`, `.table`, …) und
enthalten weder `<style>` noch `style=`-Attribute. Versandstellen rendern über
`apps.common.email.render_email(template, context)` → `(html, text)`: der Inliner (`css-inline`)
schreibt die Klassen in `style=`-Attribute, die Text-Alternative kommt aus dem `.txt`-Geschwistertemplate
oder per `html2text` aus dem HTML. `apps/common/tests/test_emails.py` rendert jede Mail mit Beispieldaten
und vergleicht mit Snapshots unter `apps/common/tests/snapshots/emails/`; nach gewollten Änderungen
`UPDATE_SNAPSHOTS=1 pytest apps/common/tests/test_emails.py` ausführen und die Snapshots mit committen.

### 5.4 Barrierefreiheit

Barrierefreiheit ist Teil jeder Komponente: Tastaturbedienung, Fokus sichtbar, ARIA, Zielgröße
mindestens 24 Pixel, Kontraste nach WCAG 2.2 AA. Dieser Abschnitt hält fest, was automatisch geprüft
wird und was nachgewiesen ist; er ist die technische Grundlage für die Barrierefreiheitserklärung nach
BITV 2.0 / EN 301 549 (Issues #176, #44, #98). Die Erklärung selbst ist ein Dokument des Betreibers;
manuelle Prüfungen mit Screenreader werden als Issue mit Label `a11y` geführt.

**Automatische Prüfung:** `tests_e2e/test_core_paths.py` lädt die Seiten im Chromium, führt axe-core
aus und lässt den Lauf fehlschlagen, sobald ein Befund der Stufen *critical* oder *serious* auftritt
(`color-contrast` bis #171 informativ). Befunde der Stufen *moderate*/*minor* stehen in der Ausgabe des
Tests (`AxeResult.describe()`). Screenshots hell/dunkel landen als CI-Artefakt
(`tests_e2e/screenshots/`). Neue Kernpfade bekommen dort einen Test (ein `goto` plus
`_assert_axe_clean`).

| Kernpfad | Seite | Test |
|---|---|---|
| Anmeldung | `/accounts/login/` (leer und mit Fehlermeldung) | `TestLoginPage` |
| Passwort zurücksetzen | `/accounts/password-reset/` | `TestLoginPage` |
| Komponentenbibliothek | `/dev/ui/` (Buttons, Karten, Formularfelder, Tabs, Modal, Alpine-Modal, Alerts) | `TestUiKit` |
| Work: Dashboard, Dokumentenliste, Aufgaben | `/work/<org>/dashboard/`, `…/documents/`, `…/tasks/` | `TestWorkPortal` |
| Session: Dashboard, Sitzungen, Vorlagen | `/session/<mandant>/`, `…/meetings/`, `…/papers/` | `TestSessionPortal` (Fixture `session_user`) |
| Session: Leitstelle einer Mandantengruppe | `/session/leitstelle/<gruppe>/` | `TestSessionPortal.test_leitstelle_barrierefrei` |
| Insight: Startseite | `/` | `TestInsightPortal` |

**Komponenten (Tastatur und Screenreader-Semantik):**

| Komponente | Verhalten | Nachweis |
|---|---|---|
| `c-ui.modal` (natives `<dialog>`) | Fokusfalle, Escape, Hintergrund inert durch den Browser; `aria-labelledby` auf die Überschrift; Schließen-Button mit `aria-label` | `test_modal_focus_and_escape` |
| `c-ui.alpine-modal` und Seitenmodals (Fraktionssitzung anlegen, Sitzungsvorbereitung ×3, KI-Vorschau, Ordner, Teilen, Aufgabenzuweisung) | `role="dialog"`, `aria-modal="true"`, Fokusfalle mit inertem Hintergrund und Scrollsperre (`x-trap.inert.noscroll`), Escape schließt, Fokus kehrt zum Auslöser zurück; Überschrift per `aria-labelledby` oder `aria-label` | `test_alpine_modal_traps_focus_and_restores_it` |
| `c-ui.tabs` / `tab` / `tab-panel` | WAI-ARIA Tabs: `role=tablist/tab/tabpanel`, `aria-selected`, `aria-controls`, Roving Tabindex, Pfeiltasten, Home/Ende, automatische Aktivierung | `test_tabs_keyboard_navigation` |
| `c-ui.button` | Sichtbarer Fokusring (`focus-visible:ring`), Mindesthöhe 40 px (Zielgröße ≥ 24 px erfüllt), Icons `aria-hidden` | UI-Kit-axe |
| Formularfelder (`c-form.*`) | `<label for>`-Verknüpfung, Fehlermeldungen als Text unter dem Feld, Passwort-Umschalter mit `aria-pressed`/`aria-label` | `test_bundle_and_password_toggle` |
| Toasts | Live-Region (`role="status"`, `aria-live="polite"`) | UI-Kit-axe |
| Deklarative Aktionen (`data-confirm`, `data-href` …, #172) | Zeilen-Links reagieren zusätzlich auf den inneren `<a>`; Tastaturnutzung über den Link, nicht über die Zeile | `tests_e2e/test_actions.py` |

Neue Komponenten kommen mit ihrem Nachweis in diese Tabelle.

**Bekannte Lücken:**

- Der Dokumenteditor (TipTap/ProseMirror) ist eine Rich-Text-Oberfläche; die Symbolleiste hat
  beschriftete Schaltflächen (`aria-label` mit Tastenkürzel), die Semantik des Editorinhalts für
  Screenreader ist aber nur so gut wie die des Browsers. Kein axe-Lauf auf der Editorseite in
  der CI (Live-Server ohne WebSocket).
- Karten (Leaflet) sind nicht tastaturbedienbar; die Listen daneben bieten dieselben Informationen.
- Session-Portal: Sitzungsdetail, Tagesordnung, Niederschrift und Sitzungsgelder laufen noch nicht in der
  axe-Prüfung (#44).
- Zielgröße 24 px ist in den Komponenten eingehalten, in älteren Seiten-Templates nicht
  flächendeckend geprüft.

## 6. API

- Neue Bereiche gehören in die eine Schnittstelle unter `/api/` mit gemeinsamer OpenAPI-Beschreibung
  ([ADR Eine Schnittstelle](adr/20261105-eine-schnittstelle-api-first.md), [SCHNITTSTELLE.md](SCHNITTSTELLE.md));
  neue mandari-Bereiche unter `/api/mandari/v<n>/`, Fehler nach RFC 9457. Bestehende `JsonResponse`-Views
  werden beim Anfassen migriert. Referenz: Session-API v1 (`apps/session/api/v1/`, Leitfaden in
  [SCHNITTSTELLE.md](SCHNITTSTELLE.md#session-api-v1)): Schemas in `schemas.py`, Auth als `Principal` in
  `auth.py`, Fehler über `Problem(...)` in `problems.py`, Router in `endpoints.py`.
  Abgelöste Pfade antworten bis zur Abschaltung mit `Deprecation`, `Sunset` und `Link: rel="successor-version"`.
- Öffentliche Schnittstellen (OParl, Fraktions-API, Session-API) folgen der
  [Release-Politik](RELEASE_POLITIK.md#6-schnittstellen-und-abkündigung): inkompatible Änderungen nur in
  neuer Version mit Ankündigungsfrist.

## 7. Betrieb und Beobachtbarkeit

- Strukturierte Logs mit Request-Kennung: `apps/common/observability.py` liefert `RequestIdMiddleware`
  (`X-Request-ID` vom Proxy oder erzeugt, im Response zurück), einen Log-Filter mit `request_id`,
  `user_id` (nur UUID), `trace_id`/`span_id` und einen JSON-Formatter. Produktion loggt JSON auf INFO,
  Entwicklung Text auf DEBUG; `LOG_FORMAT`/`LOG_LEVEL` übersteuern, `DEBUG` wird in Produktion gekappt.
  Keine personenbezogenen Inhalte in Log-Zeilen (keine E-Mails, Namen, Freitexte).
- OpenTelemetry: `OTEL_EXPORTER_OTLP_ENDPOINT` schaltet Auto-Instrumentierung (Django, psycopg, Redis,
  requests, httpx) ein; `OTEL_SERVICE_NAME` benennt den Dienst. Der Sync-Trigger an den Ingestor trägt
  `request_id` und `trace_id`, damit ein Sync der auslösenden Anfrage zugeordnet werden kann.
- Ausnahmen konkret fangen; ein `except Exception` braucht `logger.exception(...)` und einen Kommentar,
  warum das Weiterlaufen richtig ist.
- Hintergrundarbeit über Django Tasks oder Management-Kommandos, immer idempotent (Aufträge laufen mit
  `TASKS_BACKEND=journal` mindestens einmal). Wiederkehrende Arbeit als Zeitplan im Code
  (`every()`/`cron()` aus `apps/events/schedule.py`, registriert in `<app>/schedules.py`), nicht als
  Host-Cronjob oder Thread im Webprozess.
- Migrationen additiv; große Apps werden bei Gelegenheit gesquasht.

## 8. Dokumentation und Commits

- Conventional Commits auf Deutsch (`feat(session): …`, `fix(insight): …`), keine Signaturen von
  Werkzeugen oder Assistenten; Einzelheiten in [CONTRIBUTING.md](../CONTRIBUTING.md#commits-und-pull-requests).
- Architekturentscheidungen als ADR (MADR-Format) unter `docs/adr/`.
- Öffentliche Dokumentation unter docs.mandari.de; Implementierungsnotizen unter `docs/`, Regeln dafür in
  [WEITERENTWICKLUNG.md, Abschnitt 10](WEITERENTWICKLUNG.md#10-wo-was-steht).
- Keine Preise, Konditionen, Zugangsdaten oder Infrastrukturdetails im Repo, in Issues oder Commits.
