# Frontend: Komponentenbibliothek, Build-Pipeline, CSP in Stufen

- Status: angenommen
- Datum: 2026-09-09

## Kontext

Das Frontend besteht aus 300 Django-Templates mit rund 52.000 Zeilen. Etwa 17 Prozent davon sind
JavaScript und CSS in `.html`-Dateien (43 `<style>`-Blöcke, 59 Inline-`<script>`-Blöcke, 43 Alpine-
Komponentenfabriken ohne Modul), 215 Stellen interpolieren Server-Daten in Skripte. Es gibt kein
Komponentenmodell: rund 250 Card-, 144 Input- und 237 Label-Markups sind kopierte Tailwind-Klassenketten.
Die Content-Security-Policy erlaubt deshalb `unsafe-inline` und `unsafe-eval`. Ein TypeScript-Bundle mit
esbuild existiert bereits für den Editor.

## Optionen

1. **django-cotton**: HTML-artige Komponenten-Tags, Slots, Attribut-Durchreichung, reine Templates,
   flache Lernkurve, kein Konflikt mit dem `{% component %}`-Tag des Admin-Themes.
2. **django-components**: Python-Klasse plus Template plus gebündeltes JS/CSS je Komponente, typisierte
   Props, mächtiger, mehr Konzepte, Tag-Namenskollision mit dem Admin-Theme.
3. Weiter mit `{% include %}` und Klassenketten.

Build: esbuild direkt ausbauen oder Vite mit `django-vite` (Manifest, Hashing, Code-Splitting).
CSP: sofort erzwingen oder gestuft (Report-Only, Nonces, Enforce).

## Entscheidung

- **django-cotton** als Komponentenbibliothek unter `templates/cotton/` (`ui/`, `form/`, `layout/`).
  Regel: Was fünfmal identisch vorkommt, wird Komponente. django-components wird erst erwogen, wenn
  Komponenten eigenes JS/CSS bündeln müssen; Cotton-Komponenten sind dann übertragbar.
- **Django-6-Template-Partials** (`{% partialdef %}`) für HTMX-Fragmente statt eigener
  `_partial.html`-Konvention; Helfer aus `django-htmx` statt eigenem Mixin.
- **Vite mit django-vite** als Build-Pipeline für TypeScript und Tailwind (v4, CSS-first-Konfiguration
  mit Design-Tokens), Manifest-Hashing statt Zeitstempel-Cache-Busting. Alpine-Komponenten werden mit
  `Alpine.data()` in `frontend/` registriert; Server-Daten kommen per `json_script`.
- **CSP in drei Stufen**: Report-Only mit Nonce (aktiv seit diesem ADR), dann Nonces für verbleibende
  Inline-Stellen und Migration der `on*=`-Handler, dann Enforce und Ablösung der permissiven Policy am
  Reverse Proxy. Alpine-CSP-Build wird danach bewertet.
- **Strangler-Vorgehen**: neue Komponenten zuerst, dann die größten Hotspots (Editor-Skript,
  Sitzungsvorbereitung, Listen über 500 Zeilen). Alt- und Neu-Muster koexistieren; das Ratchet-Gate
  verhindert neuen Altcode.

## Folgen

- Kurzfristig mehr Dateien (Komponenten, `frontend/`), dafür weniger Duplikation und testbare Bausteine.
- Node-Toolchain wird verbindlicher Teil des Builds; Build-Fehler brechen das Image ab.
- Barrierefreiheit (WCAG 2.2 AA) wird in den Komponenten einmal gelöst statt an hunderten Stellen.
- Tailwind-v4-Migration erfordert visuelle Regressionstests (Screenshot-Vergleich der Komponentenseite).
