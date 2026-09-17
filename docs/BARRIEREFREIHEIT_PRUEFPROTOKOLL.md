# Barrierefreiheit: Prüfprotokoll der Komponenten und Kernpfade

Stand: 18.09.2026 · Issue #176 (Grundlage für #44, #98 und die Barrierefreiheitserklärung)

Dieses Protokoll hält fest, **was automatisch geprüft wird**, **was manuell geprüft wurde**
und **was offen ist**. Es ist die technische Grundlage für die Barrierefreiheitserklärung
nach BITV 2.0 / EN 301 549; die Erklärung selbst ist ein separates Dokument des Betreibers.

## 1. Automatische Prüfung (axe-core über Playwright, in jeder CI)

`tests_e2e/test_core_paths.py` lädt die Seiten im Chromium, führt axe-core aus und lässt den
Lauf fehlschlagen, sobald ein Befund der Stufen *critical* oder *serious* auftritt. Geprüft
werden (Stand dieses Protokolls):

| Kernpfad | Seite | Test |
|---|---|---|
| Anmeldung | `/accounts/login/` (leer und mit Fehlermeldung) | `TestLogin` |
| Passwort zurücksetzen | `/accounts/password-reset/` | `TestLogin` |
| Komponentenbibliothek | `/dev/ui/` (Buttons, Karten, Formularfelder, Tabs, Modal, Alpine-Modal, Alerts) | `TestUiKit` |
| Work: Dashboard, Dokumentenliste, Aufgaben | `/work/<org>/dashboard/`, `…/documents/`, `…/tasks/` | `TestWorkPortal` |
| Insight: Startseite | `/` | `TestInsightPortal` |

Ergebnis: **keine kritischen oder schweren Befunde** (Stand 18.09.2026, CI-Lauf des PR zu #176).
Befunde der Stufen *moderate*/*minor* brechen den Lauf nicht ab; sie stehen in der Ausgabe
des Tests (`AxeResult.describe()`) und sind Kandidaten für die nächste Runde.

Screenshots (hell/dunkel) landen als CI-Artefakt (`tests_e2e/screenshots/`).

## 2. Komponenten (Tastatur und Screenreader-Semantik)

| Komponente | Verhalten | Nachweis |
|---|---|---|
| `c-ui.modal` (natives `<dialog>`) | Fokusfalle, Escape, Hintergrund inert durch den Browser; `aria-labelledby` auf die Überschrift; Schließen-Button mit `aria-label` | `test_modal_focus_and_escape` |
| `c-ui.alpine-modal` und Seitenmodals (Fraktionssitzung anlegen, Sitzungsvorbereitung ×3, KI-Vorschau, Ordner, Teilen, Aufgabenzuweisung) | `role="dialog"`, `aria-modal="true"`, Fokusfalle mit inertem Hintergrund und Scrollsperre (`x-trap.inert.noscroll`), Escape schließt, Fokus kehrt zum Auslöser zurück; Überschrift per `aria-labelledby` oder `aria-label` | `test_alpine_modal_traps_focus_and_restores_it` |
| `c-ui.tabs` / `tab` / `tab-panel` | WAI-ARIA Tabs: `role=tablist/tab/tabpanel`, `aria-selected`, `aria-controls`, Roving Tabindex, Pfeiltasten, Home/Ende, automatische Aktivierung | `test_tabs_keyboard_navigation` |
| `c-ui.button` | Sichtbarer Fokusring (`focus-visible:ring`), Mindesthöhe 40 px (Zielgröße ≥ 24 px erfüllt), Icons `aria-hidden` | UI-Kit-axe |
| Formularfelder (`c-form.*`) | `<label for>`-Verknüpfung, Fehlermeldungen als Text unter dem Feld, Passwort-Umschalter mit `aria-pressed`/`aria-label` | `test_bundle_and_password_toggle` |
| Toasts | Live-Region (`role="status"`, `aria-live="polite"`) | UI-Kit-axe |
| Deklarative Aktionen (`data-confirm`, `data-href` …, #172) | Zeilen-Links reagieren zusätzlich auf den inneren `<a>`; Tastaturnutzung über den Link, nicht über die Zeile | `tests_e2e/test_actions.py` |

## 3. Manuelle Prüfung (Screenreader)

| Prüfung | Stand |
|---|---|
| NVDA + Firefox (Windows): Anmeldung, Dashboard, Dokument öffnen und bearbeiten, Modal öffnen/schließen, Tabs | **offen** – Durchlauf durch den Betreiber, Ergebnis hier eintragen |
| VoiceOver + Safari (macOS/iOS): dieselben Pfade | **offen** |
| Zoom 200 % und 400 % (Reflow ohne horizontales Scrollen) | **offen** |
| Kontrast im Dunkelmodus (axe prüft den hellen Modus in der CI; Screenshots beider Modi liegen vor) | teilweise – Screenshots vorhanden, Bewertung offen |

Vorgehen für den Durchlauf: je Pfad notieren, ob alle Bedienelemente erreichbar und
verständlich angesagt werden, ob der Fokus sichtbar ist und ob Statusänderungen (Speichern,
Fehler) angesagt werden. Befunde als Issue mit Label `a11y` anlegen.

## 4. Bekannte Lücken

- Der Dokumenteditor (Tiptap/ProseMirror) ist eine Rich-Text-Oberfläche; die Symbolleiste hat
  beschriftete Schaltflächen (`aria-label` mit Tastenkürzel), die Semantik des Editorinhalts für
  Screenreader ist aber nur so gut wie die des Browsers. Kein axe-Lauf auf der Editorseite in
  der CI (Live-Server ohne WebSocket); manuell prüfen.
- Insight-Karten (MapLibre) sind nicht tastaturbedienbar; die Listen daneben bieten dieselben
  Informationen.
- Session-Portal: axe-Lauf in der CI fehlt noch (Mandanten-Fixture für E2E), siehe #44.
- Zielgröße 24 px ist in den Komponenten eingehalten, in älteren Seiten-Templates nicht
  flächendeckend geprüft.

## 5. Pflege

Neue Komponenten kommen mit ihrem Nachweis in Abschnitt 2, neue Kernpfade in die Tabelle in
Abschnitt 1 (ein `goto` plus `_assert_axe_clean` im E2E-Test). Ein manueller Durchlauf ist
nach größeren UI-Änderungen zu wiederholen; Datum und Ergebnis in Abschnitt 3 eintragen.
