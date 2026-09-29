# Kartenanalyse im Dateicache (Issue #599)

Viele Anlagen zu Vorlagen enthalten Karten und Pläne: Lagepläne, Bebauungspläne, FNP-Ausschnitte,
Straßenplanungen. Bevor ein Dokument-Worker sie dauerhaft erkennt, rendert und verortet, misst
`analyze_maps` den Bestand – nur aus dem lokalen Dateicache, ohne einen einzigen Abruf beim
Ratsinformationssystem und ohne etwas zu speichern.

## Bausteine

| Baustein | Ort | Aufgabe |
|----------|-----|---------|
| Seitenanalyse | `apps/common/documents/page_analysis.py` | fachfrei (Plattform-Schicht): Messwerte je Seite, Einordnung als Karte |
| Auswertung | `insight_core/services/map_survey.py` | Dateien der Kommune aus dem Cache, Straßenverzeichnis, Kennzahlen, Stichprobe |
| Befehl | `insight_core/management/commands/analyze_maps.py` | Aufruf, Ausgabe, JSON, Stichprobe, Auswertung der Prüfung von Hand |

Bibliotheken: pypdfium2 (PDFium) und pypdf, nicht PyMuPDF – Begründung in
[`docs/adr/20260930-pdf-seitenanalyse-bibliothek.md`](adr/20260930-pdf-seitenanalyse-bibliothek.md).

## Aufruf

```bash
# Gesamter Cache der Kommune (neueste Dateien zuerst); nur Auswertung, --dry-run ist Pflicht
python manage.py analyze_maps --body muenster --dry-run
# Teilmenge, Kennzahlen als JSON
python manage.py analyze_maps --body muenster --dry-run --limit 2000 --json /tmp/karten.json
# Stichprobe für die Prüfung von Hand: je zur Hälfte vorhergesagte Karten und andere Seiten
python manage.py analyze_maps --body muenster --dry-run --sample-csv /tmp/stichprobe.csv --sample-size 200
# Spalte „label“ mit ja/nein füllen, dann Präzision und Trefferquote berechnen
python manage.py analyze_maps --evaluate /tmp/stichprobe.csv
```

Der Befehl hält eine Singleton-Sperre (`--ohne-sperre` erzwingt). Er läuft im Web-Container und liest
Datei für Datei; `--max-mb` (Vorgabe `FILE_CACHE_MAX_MB`) und `--max-pages` (Vorgabe 300) begrenzen
große Dokumente. Nur öffentliche Dateien: gelöschte und von mandari Session zurückgenommene Dateien
bleiben außen vor.

## Kennzahlen

- Dateien und Seiten, davon Karten bzw. Pläne (Anteil), Dateien mit mindestens einer Kartenseite
- Kartenseiten mit maschinenlesbarem Maßstab (`/VP` mit `/Measure /RL`, AutoCAD-Plots), als GeoPDF,
  mit Koordinatenbeschriftung (Rechts-/Hochwert-Paare im Textlayer), mit mindestens vier Straßennamen
  aus dem Straßenverzeichnis der Kommune, als Scan ohne Textlayer, mit 1-bit-Rastergrundkarte
- Median der effektiven Rasterauflösung, Papierformate, häufigste Erzeuger (Creator/Producer)
- Übersprungene Dateien (nicht im Cache, kein PDF, zu groß, nicht lesbar), Laufzeit, höchster
  Speicherbedarf des Prozesses (Linux)

## Messwerte je Seite und Einordnung

`analyze_pdf` misst Format, Pfadobjekte und -segmente (auch in Form-XObjects), Vektordichte (Segmente
je cm²), Rasterbilder (Pixel, Bittiefe, Filter, effektive dpi, Flächenanteil), den Textlayer (auf
Wunsch mit Rechtecken), Kartenstichworte, Koordinatenpaare, Maßstäbe aus `/VP` (der Papierbereich
entfällt) und GeoPDF-Merkmale (`/Measure /GEO`, `/LGIDict`). Bildseiten bekommen eine grobe Vorschau
(40 dpi), um Karten (wenige Farbtöne) von Fotos zu trennen.

`classify_page` vergibt Punkte nach den Regeln aus dem Machbarkeitstest (Stufe C), Karte ab 3 Punkten:

| Merkmal | Punkte |
|---------|--------|
| Vektorzeichnung (≥ 5.000 Segmente und ≥ 10 je cm²) | +2 |
| Maßstab im PDF (`/VP`) | +2 |
| GeoPDF | +3 |
| größer als A4 | +1 |
| ≥ 2 bzw. ≥ 4 Kartenstichworte | +1 bzw. +2 |
| ≥ 2 Straßennamen aus dem Straßenverzeichnis | +1 |
| Textseite (> 1.500 Zeichen, kaum Vektoren) | −3 |
| Bildseite: wie eine Karte (wenige Farben, 1-bit-Grundkarte, Vektoren oder `/VP`) bzw. wie ein Foto | +2 bzw. −2 |

Auf den 44 Seiten der Stichprobe des Machbarkeitstests (18 PDFs, Münster) ergibt das ohne
Straßenverzeichnis Präzision 0,92 und Trefferquote 0,79 (11 von 14 Karten). Verpasst werden wie im
Test der Scan ohne Textlayer, der Nachdruck mit in Pfade umgewandelter Schrift und ein A0-Plan mit
geringer Vektordichte, den im Machbarkeitstest erst die Straßennamen über die Schwelle hoben; Fehlalarm
bleibt der Querschnitt. Die Regeln sind an dieser Stichprobe entstanden – die Prüfung von Hand an 200 Seiten
aus dem Bestand (Akzeptanzkriterium: Präzision ≥ 0,9, Trefferquote ≥ 0,8) steht aus.

## Stichprobe und Prüfung von Hand

Die Stichprobe zieht je zur Hälfte Seiten, die als Karte eingeordnet wurden, und andere Seiten
(Zufallsauswahl über den ganzen Lauf). Die Spalte `weight` gibt an, für wie viele Seiten des Bestands
eine Seite steht. `--evaluate` rechnet Präzision und Trefferquote einmal für die Stichprobe und einmal
nach Schichten gewichtet – die gewichtete Trefferquote ist die Schätzung für den Bestand, weil Karten
in der Stichprobe überrepräsentiert sind.

## Folgeschritte (Dokument-Worker)

Noch nicht umgesetzt, weil sie auf dem Auftrags-Backend (A4, #506) und der einen Texterkennung (#530)
aufbauen:

- Auftrag `docs.analyze_pages` in der Warteschlange `ocr`, ausgelöst durch neue bzw. geänderte
  öffentliche Dateien; Ergebnis beim Eigentümer als `ris.file.maps_extracted` (ADR A5)
- Modell für erkannte Karten (Datei, Seite, Art, Maßstab, Rahmen, Rendering, Merkmale)
- Rendern des Kartenausschnitts (≤ 200 dpi, WebP, bei Bedarf Kacheln) unter
  `<kommune>/<jahr>/<datei-id>/karten/` neben dem Dateicache
- Löschen bzw. Depublizieren einer Datei entfernt die Renderings (Löschkonzept)
- Speicherbedarf des Workers ≤ 1 GB beim größten Plan im Bestand messen (Rendern mit 200 dpi statt
  300 dpi, Kacheln)
