# ALLRIS-3-Testseiten

Testseiten für den ALLRIS-3-Adapter (`src/scrapers/allris3.py`, Issue #121), aufgebaut wie die
Golden Files unter `../sessionnet/`: `expected/*.json` ist die eingefrorene Parser-Ausgabe
(`tests/test_allris3_parser.py`). Für diese Seiten hat mandari keinen ALLRIS-Server abgefragt; die
Tests laufen ohne Netz.

## Herkunft

Alle Seiten in den Ordnern der Instanzen und unter `cases/` stammen unverändert aus dem Projekt
[bv-hh](https://github.com/bv-hh/bv-hh) (Commit `91dbf42a119cfc168ae67c50fc1213fb74738237`,
Verzeichnisse `test/fixtures/files/allris/` und `test/fixtures/files/`), das sie von öffentlichen
Instanzen mit ALLRIS net 3.9.5 aufgenommen hat. Ordner- und Dateinamen folgen bv-hh. Die Bytes sind
so, wie die Server sie ausliefern (Windows-1252, Zeilenenden CRLF); `.gitattributes` verhindert jede
Umwandlung. Die Inhalte sind amtliche, öffentliche Ratsinformationen und dienen hier nur zum Testen.

| Datei | Seite |
|---|---|
| `<instanz>/vo020.html` | Drucksache mit Sachverhalt und Beschlussvorschlag |
| `<instanz>/to010.html` | Sitzung mit Tagesordnung |
| `<instanz>/to020.html` | Tagesordnungspunkt mit Protokoll |
| `<instanz>/au020.html` | Ausschuss mit Mitgliedern |
| `<instanz>/fr020.html` | Fraktion mit Mitgliedern |
| `hamburg_nord/si010_e.html` | Sitzungskalender Oktober 2023 |
| `hamburg_nord/vo040.html` | Drucksachenliste (erste Seite, neueste zuerst) |
| `hamburg_nord/fr010.html` | Fraktionsliste |
| `hamburg_nord/to010_pa.html` | Sitzung eines Hauptorgans (Verweis `pa020`); in bv-hh `test/fixtures/files/to010.html` |
| `cases/*.html` | Drucksachen mit Sonderfällen der Abschnitte (Platzhalter, Fußnoten, Tabellen, Zeichen der Schrift Wingdings) |

bv-hh steht unter der MIT-Lizenz (`LICENSES/MIT.txt`, Zuordnung in `REUSE.toml`):

```text
MIT License

Copyright (c) 2020, 2021 Timo B. Kranz

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Synthetische Seiten

`synthetisch/` enthält erfundene Seiten im Aufbau der übernommenen (Rahmen, Klassen, Tabellen),
ohne echte Namen und Inhalte. Sie sind im Quelltext als synthetisch gekennzeichnet und in
Windows-1252 gespeichert:

| Datei | Zweck |
|---|---|
| `pa020.html` | Hauptorgan mit Mitgliedern (bv-hh hat keine `pa020`-Seite); Zeichen aus Windows-1252 (Gedankenstrich, Anführungszeichen) |
| `vo020_nicht_oeffentlich.html` | Drucksache, die ALLRIS nicht herausgibt („Keine Information verfügbar“) |
| `noauth.html` | Anmeldeseite, auf die ALLRIS nicht öffentliche Seiten umleitet (Inhalt erfunden) |
