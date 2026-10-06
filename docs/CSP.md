# Content-Security-Policy (CSP)

Implementierungsnotiz zu Issue #172: Stand der Policy, Muster für Templates und Frontend-Code,
verbleibende Inline-Stellen und die Bewertung des Alpine-CSP-Builds.

Stand: 30.09.2026.

## 1. Stand

- **Policy:** `SECURE_CSP_REPORT_ONLY` in `mandari/mandari/settings.py` (Django 6). Der Browser meldet
  Verstöße an `/csp-report/` (`apps/common/csp.py`, Logger `mandari.csp`, Zähler
  `mandari_csp_violations_total{directive}`), blockiert aber nichts. Erzwungen wird bislang nur die
  permissive Policy des Reverse Proxys.
- **script-src:** `'self'`, Nonce, `'unsafe-eval'`. Der Nonce steht an allen Inline-Skripten
  (`nonce="{{ csp_nonce }}"`, Kontextprozessor `django.template.context_processors.csp`).
  `'unsafe-eval'` braucht Alpine (Ausdrücke in `x-data`, `@click`, `:class` …), siehe Abschnitt 5.
- **style-src:** `'self'`, `'unsafe-inline'`. Seit diesem Schritt tragen alle `<style>`-Blöcke in
  Templates, die ein Browser erhält (Layouts, Admin, Entwicklungsseiten, PWA und die verbleibenden
  Seiten), ebenfalls den Nonce. Wirksam wird das erst, wenn `style-src` einen Nonce verlangt – solange
  `'unsafe-inline'` ohne Nonce in der Liste steht, ändert sich nichts.
- **Ohne Nonce bleiben** E-Mail-Vorlagen (`emails/`, `work/notifications/email/`) und Vorlagen, die nur
  serverseitig zu PDF oder Exportdateien gerendert werden (`*/pdf/`, `work/motions/export/pdf_template.html`,
  `work/profile/export/`). Sie werden ohne Request gerendert und nie unter einer CSP ausgewertet.
- **Fehlerseite 500** (`errors/base_error.html`): wird absichtlich ohne Request-Kontext gerendert
  (`mandari/urls.py`, `handler_500`), der Nonce ist dort leer. Sobald `style-src` einen Nonce verlangt,
  braucht die Seite ihre Styles aus `styles.css` oder einen eigenen Nonce.
- **Inline-Handler:** keine (`on*=`, `hx-on` – Ratchet `on_handlers` = 0). Aktionen laufen über
  `data-*`-Attribute (`frontend/js/actions.ts`, `frontend/js/htmx-setup.ts`).
- **htmx:** `htmx.config.allowEval = false`, `selfRequestsOnly = true`; keine `hx-vars`, keine
  `hx-vals="js:…"`, keine Trigger-Filter mit `[…]` (geprüft 30.09.2026).
- **Frontend-Ratchet** (`scripts/check_frontend_ratchet.py`, nur sinkend): Inline-Skripte in
  Seiten-Templates 35 → 13, Inline-Styles 19 → 16, `style=`-Attribute 129 → 100.

## 2. Muster für Seiten ohne Inline-Code

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
- **Tests:** Jede ausgelagerte Komponente bekommt einen E2E-Test mit der Fixture `problems`
  (`tests_e2e/conftest.py`: Alpine-Fehler, Ausnahmen, Serverfehler) und prüft die typische Bedienung.

## 3. In Schritt 2 umgestellte Templates

| Template | jetzt |
|---|---|
| `work/organization/parties.html` | `partyManager` |
| `work/organization/settings.html` | `logoPreview`, `colorPicker` (Startfarbe per `data-color`) |
| `work/organization/role_form.html` | `permissionsManager`, `colorPicker`, Löschen per `data-action="confirm-submit"` |
| `work/profile/change_requests.html` | `changeRequestForm` |
| `work/profile/data_privacy.html` | `dataExport`, Exporte per `json_script` |
| `work/profile/visibility.html` | reines CSS (`has-[:checked]`, `peer-checked`) |
| `work/support/create.html` | `ticketForm`, `kbSuggestions` |
| `work/support/kb_article.html` | `feedbackWidget` |
| `work/tasks/create.html` | `assignmentHandler` |
| `work/motions/create.html` | `createMotion`, Vorlagen per `json_script` |
| `work/motions/settings/letterhead_form.html` | `letterheadForm`, Startwerte per `json_script` |
| `work/motions/settings/type_form.html` | `data-slug-target` / `data-slug-field` |
| `pages/portal/select_body.html` | `bodySelectApp`, Style nach `input.css` |
| `pages/merkliste.html` | `merklisteController` |
| `pages/persons/ask_question.html` | `questionForm` |
| `pages/subscribe.html`, `pages/subscription_manage.html` | `neighborhoodSubscription` |
| `pages/meetings/year_plan.html` | Druck-Styles nach `input.css` |
| `session/partials/textblock_picker.html` | `form-behaviors.ts` (delegiert) |
| `session/papers/consultation_section.html` | `data-filter-options` |
| `session/settings/roles.html` | Häkchen serverseitig (`checked`) |
| `feedback/report.html` | `data-browser-info` |
| `pages/offline.html` → `pwa/offline.html` | bewusst eigenständig (vom Service Worker vorgecacht), Skript und Style mit Nonce |

Dazu ohne Ratchet-Wirkung: die aktive Markierung der Insight-Seitenleiste (`nav-active-bar` statt
`style`-Attribut) und das gestaffelte Einblenden der Profilkarten (`fade-delay-*`).

Nebenbei behobene Fehler (vorher im Browser defekt):

- Neues Support-Ticket: Artikelvorschläge erschienen nie (Kopplung über die Alpine-2-API `__x`).
- Neue Aufgabe: Zuweisen an eine andere Person brach mit einer Ausnahme ab, der Bestätigungsdialog
  erschien nie (`this.$el` war das Select, siehe Abschnitt 2).
- Profil → Kontaktweg: Umschalten warf eine Ausnahme (Icons sind nach dem Rendern `<svg>`, das Skript
  suchte `<i>`), die Markierung blieb stehen.
- Rollen-Rechte: „Alle“ einer Kategorie folgte einzelnen Häkchen nicht und wurde nie „unbestimmt“
  (`:indeterminate.prop` gibt es in Alpine 3 nicht).
- Abo verwalten: gespeicherte Koordinaten kamen lokalisiert zurück („51,96…“), das Speichern endete
  mit einem Serverfehler. Der Server liest Koordinaten jetzt tolerant.

## 4. Verbleibende Inline-Stellen

Inline-Skripte (13, alle mit Nonce):

| Template | Inhalt | Plan |
|---|---|---|
| `admin/index.html` | Diagramme (Chart.js) im Admin | eigenes Modul; Admin braucht ohnehin eine eigene Policy (Abschnitt 5) |
| `components/chatbot_popup.html`, `pages/chat.html` | Chat mit Markdown-Ausgabe (`x-html`) | zusammen mit dem Alpine-CSP-Build (x-html ersetzen) |
| `pages/map.html`, `pages/neighborhood.html`, `pages/portal/home.html`, `pages/meetings/detail.html` | Leaflet-Karten | gemeinsames Kartenmodul in `frontend/js/`, Leaflet als Vendor-Skript vorher geladen (die Karte der Recherche in Work ist seit #853 die Alpine-Komponente `risKarte`) |
| `pages/meetings/calendar.html`, `work/meetings/calendar.html` | FullCalendar | gemeinsames Kalendermodul |
| `work/meetings/teleprompter.html` | Teleprompter | Alpine-Komponente |
| `session/meetings/detail.html` | Drag-and-drop der Tagesordnung | Verhalten `data-reorder-url` in `form-behaviors.ts` |
| `work/support/detail.html` | Nachrichten-Polling (Zähler im `hx-get`) | Zähler per `htmx:configRequest` statt Attribut-Umschreiben |

Inline-Styles (16): Layout-nahe Blöcke (`accounts/base_auth.html`, `errors/base_error.html`,
`admin/_monitor_styles.html`), Karten- und Kalenderseiten, Sitzungsvorbereitung und Teleprompter,
die Editor-Styles (`work/motions/partials/_editor_styles.html`, rund 1.000 Zeilen) sowie PDF- und
Exportvorlagen (bleiben). Alle per HTTP ausgelieferten tragen den Nonce.

## 5. Bewertung: Alpine-CSP-Build (`@alpinejs/csp`)

### Was der Build ändert

`@alpinejs/csp` (geprüft: 3.17.4, gleiche Version wie `alpinejs`) ersetzt die Auswertung per
`new Function` durch einen eigenen Ausdrucksparser. Direktiven, Magics, Plugins (`collapse`, `focus`)
und `Alpine.data` bleiben gleich. Erlaubt sind in Ausdrücken:

- Eigenschaftszugriff, Methodenaufrufe mit Argumenten (`toggle(item.id)`, `$dispatch('x', { id: 1 })`)
- Vergleichs-, Logik- und Rechenoperatoren, Ternär-Operator, einfache Zuweisung, `++`/`--`
- Objekt- und Array-Literale, auch als `x-data="{ offen: false }"`

Nicht erlaubt:

- mehrere Anweisungen (`a = 1; b = 2`), `if (…) { … }`
- Pfeilfunktionen, Template-Literale, `?.`, `??`, `+=`, `typeof`, `new`, `in`, Spread
- globale Namen: `window`, `document`, `localStorage`, `JSON`, `Math`, `console`, aber auch
  `confirmAction`, `showToast` und Funktionen aus Inline-Skripten
- Zuweisungen an DOM-Eigenschaften (`$refs.feld.value = ''`, `$el.style.height = …`) und Aufrufe wie
  `setAttribute`, `appendChild`
- `x-html` ist abgeschaltet

Unzulässige Ausdrücke fallen erst im Browser auf (Konsolenfehler, die Bedienung reagiert nicht) –
es gibt keinen Build-Fehler.

### Messung

Der Parser des Builds wurde über alle Alpine-Attribute der Templates laufen gelassen (Werte vom
30.09.2026, nach diesem Schritt):

| | Anzahl |
|---|---|
| Alpine-Ausdrücke in 150 Templates | 1.833 |
| laufen unverändert | 1.691 (92 %) |
| müssen umgeschrieben werden | 142 in 67 Templates |
| davon Anweisungsfolgen / `if` | 58 |
| davon Pfeilfunktionen (meist `.then(ok => …)` nach `confirmAction`) | 44 |
| davon Methoden in Inline-`x-data`-Objekten | 14 |
| davon globale Namen (`localStorage` 7, `JSON`, `Math` …) | 13 |
| davon Template-Literale | 9 |
| `x-html` | 9 (Chat, KI-Vorschau und Versionsvergleich im Editor, Zusammenfassung in der Sitzungsvorbereitung) |

Schwerpunkte: Editor (Toolbar, Popups, Kopf: rund 22), Work-Layout (8), Sitzungsvorbereitung (rund 13),
Panels der Fraktionssitzung. Dazu kommen die 13 Seiten mit Inline-Skripten aus Abschnitt 4, deren
`x-data="funktion()"` auf globale Funktionen zeigen. Die frühere Schätzung („rund 1.500 Ausdrücke,
mehrwöchig“) ist damit überholt: Der Großteil der Ausdrücke ist mit dem heutigen Parser gültig.

### Was sich außerhalb der Templates ändern müsste

- `import Alpine from 'alpinejs'` → `@alpinejs/csp` in `main.ts`, `work.ts` und im Editor-Bundle
  (eine Instanz für alle Einstiege).
- **Django-Admin (Unfold)** bringt ein eigenes Alpine mit, das `eval` braucht. Optionen: für `/admin/`
  eine eigene Policy per `django.views.decorators.csp.csp_override` bzw. Middleware-Ausnahme, oder
  Unfolds Alpine gegen den CSP-Build tauschen (Aufwand und Wartbarkeit offen).
- **CI-Prüfung:** ein Skript, das den Parser des Builds über alle Templates laufen lässt (wie für diese
  Messung) und als Ratchet (nur sinkend) im Job „Qualität“ läuft. Ohne diese Prüfung kommen neue
  unzulässige Ausdrücke unbemerkt hinzu.
- `x-html` durch Komponentenmethoden ersetzen, die bereinigtes HTML per `x-ref` einsetzen.

### Aufwand

- Prüfskript und Ratchet: 0,5 Tage.
- 142 Ausdrücke umschreiben (Methoden in den Komponenten, Anweisungsfolgen in Methoden, Bestätigungen
  über `data-action="confirm-submit"` bzw. Komponentenmethoden): 3–4 Tage einschließlich E2E-Tests je Seite.
- 13 verbleibende Inline-Skripte (Karten, Kalender, Chat, Admin-Diagramme): 2–3 Tage.
- Umstellung, Admin-Policy, Report-Only-Messung: 1 Tag plus 14 Tage Beobachtung.

Zusammen etwa 7–9 Arbeitstage, verteilt auf mehrere PRs.

### Risiken

- **Editor und Sitzungsvorbereitung:** meiste Ausdrücke, TipTap-Instanzen, Start-Reihenfolge. Hier zuletzt
  umstellen, nur mit E2E-Abdeckung (`tests_e2e/test_editor*.py`).
- **Stille Fehler:** Ein unzulässiger Ausdruck wirft zur Laufzeit; ohne E2E-Test der Seite fällt das nicht
  auf. Die Fixture `problems` erkennt Alpine-Fehler in der Konsole.
- **Leistung:** Der Build parst einen Ausdruck bei jeder Auswertung neu (3.17.4). Große Listen
  (`x-for` in Kanban, Sitzungsvorbereitung) vorher messen.
- **Drittbibliotheken** (Leaflet, FullCalendar, Chart.js, pdf.js) laufen außerhalb von Alpine; ob sie ohne
  `eval` auskommen, zeigt erst die Report-Only-Phase ohne `'unsafe-eval'`.

### Schutzwirkung ohne CSP-Build

Eine erzwungene Policy mit Nonce und `'unsafe-eval'` (aber ohne `'unsafe-inline'`) blockiert
eingeschleuste `<script>`-Elemente und Inline-Handler. Alpine wertet aber auch Attribute aus Markup aus,
das nachträglich ins DOM kommt; gegen eingeschleustes Markup mit Alpine-Attributen hilft die Policy
dann nicht. Der CSP-Build schränkt das deutlich ein (keine globalen Objekte, keine DOM-Zuweisungen in
Ausdrücken), vollständig schließt er es nicht.

### Empfehlung

1. **Enforce zuerst mit `'unsafe-eval'`** (Teil 3 von #172): nach 14 Tagen Report-Only ohne offene
   Verstöße `SECURE_CSP` mit Nonce, ohne `'unsafe-inline'` für Skripte, Proxy-Policy ablösen.
2. **CSP-Build als eigenes Vorhaben**, stufenweise: Prüfskript mit Ratchet → Insight und Session →
   Work ohne Editor → Editor und Sitzungsvorbereitung → Import umstellen und `'unsafe-eval'` zunächst in
   der Report-Only-Policy streichen → Admin-Policy → Enforce ohne `'unsafe-eval'`.
3. **Neue Templates** ab sofort CSP-Build-tauglich schreiben (Abschnitt 2), damit die Zahl nicht wächst.

## 6. Checkliste bis Enforce

- [ ] 14 Tage Report-Only ohne offene Verstöße (Protokoll `mandari.csp`, Zähler je Direktive)
- [ ] `SECURE_CSP` mit Nonce für `script-src`, `'unsafe-eval'` vorerst behalten
- [ ] Entscheidung `style-src`: `'unsafe-inline'` behalten oder `style-src-elem` mit Nonce und
      `style-src-attr 'unsafe-inline'` (100 `style=`-Attribute, Alpine-Bindings per CSSOM sind nicht betroffen)
- [ ] Proxy-Policy entfernen
- [ ] Playwright-Kernpfade grün unter erzwungener Policy
