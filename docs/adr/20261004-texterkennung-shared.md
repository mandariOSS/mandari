# Texterkennung als eine Bibliothek in `shared/` und als Auftrag `file.extract_text`

- Status: angenommen
- Datum: 2026-10-04
- Issue: #530 (Epic #476), Vorarbeit #817
- Bezug: [A1 Schichtenmodell](20260929-schichtenmodell.md), [A4 Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md),
  [PDF-Seitenanalyse](20260930-pdf-seitenanalyse-bibliothek.md)

## Kontext

Text aus Dokumenten erkannte mandari an zwei Stellen mit zwei Implementierungen: im OCR-Worker des Ingestors
(`extract-daemon`, seitenweise) und in der Anwendung (`insight_core/services/document_extraction.py`, alle
Seiten auf einmal, mit eigener asynchroner Mistral-Anbindung). Beide brauchten die Systemwerkzeuge Poppler und
Tesseract, beide hatten eigene Grenzen. Der OCR-Worker wurde vom Speicherwächter des Kernels beendet (#817);
die Grenzen je Seite und Datei mussten danach an zwei Stellen gepflegt werden. ADR A4 sieht für Texterkennung
eine eigene Warteschlange `ocr` im Worker der Anwendung vor.

## Entscheidung

- **Eine Bibliothek** `mandari_dokumente` in `shared/` (Distribution `mandari-oparl`, zweites Paket neben
  `mandari_oparl`), ohne Django- oder Datenbankbezug: `extract_text(Pfad oder Bytes, MIME-Typ, Dateiname,
  ExtractionConfig) -> ExtractionResult` mit der Kette pypdf → Mistral (optional) → Tesseract. Tesseract
  läuft Seite für Seite als Unterprozess mit Speicher- und Zeitgrenze und gedeckelter Auflösung je
  Seitengröße (`mandari_dokumente.ocr`, aus #817). Eine Mistral-Anbindung (`mandari_dokumente.mistral`),
  synchron, mit Begrenzung je Minute und Prozess.
- **Aufrufer behalten Abruf und Speichern**: Der Ingestor lädt wie bisher selbst (Drossel, robots.txt,
  Dokumentablage), die Anwendung über `document_extraction` (Drossel, robots.txt, nur öffentliche Ziele).
  Einstellungen heißen in beiden gleich (`OCR_*`, `MISTRAL_*`, `TEXT_EXTRACTION_*`).
- **Auftrag `file.extract_text`** (`insight_core.background_tasks.file_extract_text`, Warteschlange `ocr`):
  Texterkennung einer RIS-Datei im Worker der Anwendung, mit denselben Regeln für Abbrüche wie im Ingestor
  (Versuch zählen, nach Zeitablauf zurückstellen, nach mehreren Abbrüchen „Speichergrenze“). Der Zeitplan
  `texterkennung_einplanen` beansprucht wartende Dateien und reiht Aufträge ein, höchstens
  `TEXT_EXTRACTION_QUEUE_DEPTH` gleichzeitig.
- **Ein Schalter, kein Doppelbetrieb**: `TEXT_EXTRACTION_RUNNER` (`ingestor` = OCR-Worker des Ingestors,
  Standard und bisheriges Verhalten; `worker` = Aufträge der Anwendung). Mit `worker` ruht die Texterkennung
  im Ingestor; der Zeitplan ist nur dann eingeplant. Welcher Worker die Warteschlange `ocr` bedient
  (`worker-heavy` oder ein anderer), bleibt der Installation überlassen.
- Die PDF-Seitenanalyse (`apps/common/documents/page_analysis.py`) bleibt vorerst, wo sie ist; sie kann später
  in dieselbe Bibliothek ziehen.

## Alternativen

- **Texterkennung in `mandari_oparl`.** Ein Paket weniger, aber `mandari_oparl` beschreibt OParl-Typen;
  Dokumente sind ein eigenes Thema. Verworfen.
- **Eigene Distribution für die Dokumentbibliothek.** Saubere Trennung, aber ein weiterer Pfad in
  `[tool.uv.sources]`, beiden Lock-Dateien und beiden Images. Verworfen, solange beide Pakete zusammen
  ausgeliefert werden.
- **Django-Implementierung behalten und dem Ingestor geben.** Sie rendert alle Seiten auf einmal und hängt an
  Django-Einstellungen. Verworfen.
- **Sofort ganz auf Aufträge umstellen.** Ändert Betrieb und Last ohne Rückweg. Verworfen zugunsten des
  Schalters mit heutigem Standard.

## Folgen

**Positiv**

- Grenzen, Fehlerverhalten und Mistral-Anbindung gibt es einmal; Verbesserungen wirken in Ingestor und
  Anwendung.
- Die Anwendung braucht weder `pdf2image` noch `pytesseract`; beide Pakete sind entfernt.
- Die Texterkennung kann schrittweise in den Worker der Anwendung umziehen, mit einer Variablen als Rückweg.

**Negativ**

- `shared/` hat nun Abhängigkeiten (pypdf, httpx); Änderungen daran berühren beide Lock-Dateien.
- Bis zum vollständigen Umzug gibt es zwei Ausführungsorte; der Schalter muss in Anwendung und Ingestor gleich
  stehen.

## Prüfung (Fitnessfunktion)

- `insight_core/tests/test_ocr_seitenweise.py`: Die Anwendung hat keine eigene Texterkennung und keine eigene
  Mistral-Anbindung mehr.
- `ingestor/tests/test_ocr_grenzen.py` und `ingestor/tests/test_texterkennung_shared.py` prüfen die Bibliothek,
  in der CI mit Poppler und Tesseract.
