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

Seiten je Instanz:

- `si0040.html` — Sitzungskalender (Monatsansicht)
- `si0050.html` — Sitzungsdetail, Tab "Informationen"
- `si0057.html` — Sitzungsdetail, Tab "Tagesordnung"
- `vo0050.html` — Vorlagendetail
- `gr0040.html` — Gremienliste
- `kp0040.html` — Gremium-Mitglieder

Die Inhalte sind amtliche öffentliche Ratsinformationen; die Fixtures dienen
ausschließlich Testzwecken (Quellenangabe: jeweilige Kommune).
