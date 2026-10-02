# Scraper-Fixtures (Golden Files)

Eingefrorene HTML-Seiten realer, öffentlicher RIS-Instanzen für die
Golden-File-Regressionstests der Scraper-Adapter (siehe
`tests/test_sessionnet_parser.py`). Die `expected/*.json`-Dateien sind die
eingefrorene Parser-Ausgabe — schlägt ein Vergleich fehl, hat ein Refactor
Felder verloren (oder das erwartete Ergebnis wurde bewusst aktualisiert).

## sessionnet/

Abgerufen am 2026-07-20 mit User-Agent
`mandari-ingestor (+https://mandari.de/crawler)`.

| Verzeichnis | Instanz | Variante | Version |
|---|---|---|---|
| `luedenscheid/` | https://buergerinfo.luedenscheid.de/ | `*.asp` | SessionNet 5.5.4 KP4 (Layout 6) |
| `eschweiler/` | https://rat.eschweiler.de/bi/ | `*.php` | SessionNet 5.x (gleiches Markup) |
| `samtgemeinde/` | pseudonymisiert (`ratsinfo.musterheide.example`) | `*.asp` | SessionNet 5.5, mehrere Mandanten |

Seiten je Instanz:

- `si0040.html` — Sitzungskalender (Monatsansicht)
- `si0050.html` — Sitzungsdetail, Tab "Informationen"
- `si0057.html` — Sitzungsdetail, Tab "Tagesordnung"
- `vo0050.html` — Vorlagendetail
- `gr0040.html` — Gremienliste
- `kp0040.html` — Gremium-Mitglieder

`samtgemeinde/` bildet eine Instanz mit mehreren Körperschaften nach (Samtgemeinde,
zwei Mitgliedsgemeinden, eine kommunale Gesellschaft; Auswahl über `__cpanr`). Das Markup
der Filterleiste (Mandanten- und Wahlperiodenauswahl), der Gremienliste und des Kalenders mit
der Spalte „Mandant“ folgt einer öffentlichen SessionNet-5.5-Instanz (abgerufen am
2026-10-02 mit unserem User-Agent, robots.txt geprüft); Namen, Orte, Nummern und Inhalte sind
erfunden, die Seiten gekürzt. Dateinamen tragen die Kennungsparameter
(`gr0040_cpanr2.html`, `si0040_cpanr1_2026-09.html`, `si0057_1001.html`, …). Der
nichtöffentliche Teil einer Sitzung zählt dort neu (`Ö 1`, `Ö 2`, `N 1`).

Die Inhalte von `luedenscheid/` und `eschweiler/` sind amtliche öffentliche
Ratsinformationen; alle Fixtures dienen ausschließlich Testzwecken (Quellenangabe:
jeweilige Kommune).
