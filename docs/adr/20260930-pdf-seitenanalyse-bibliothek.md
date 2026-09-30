# PDF-Seitenanalyse mit pypdfium2 und pypdf statt PyMuPDF

- Status: angenommen
- Datum: 2026-09-30
- Issue: #599
- Bezug: Machbarkeitstest „Karten aus PDF-Anlagen“ (#594), Texterkennung (#530),
  [A1 Schichtenmodell](20260929-schichtenmodell.md), [A4 Aufträge](20260929-auftraege-und-zeitplaene.md)

## Kontext

Um Karten und Pläne in PDF-Anlagen zu erkennen, zu rendern und später zu georeferenzieren, braucht
mandari mehr als Fließtext: Seitenobjekte (Vektorpfade, Rasterbilder mit Auflösung), Text mit
Positionen, Seitenverzeichnisse (`/VP` mit `/Measure` für den Maßstab von AutoCAD-Plots, `/LGIDict`
für GeoPDF) und ein schnelles, speicherschonendes Rendern. Heute nutzt mandari nur pypdf (Text) und
pdf2image/Poppler (Rendern für Tesseract).

Der Machbarkeitstest lief mit PyMuPDF. PyMuPDF steht unter der GNU Affero General Public License
(Version 3) oder einer kommerziellen Lizenz von Artifex. mandari selbst ist AGPL-3.0-or-later; die
Lizenzen sind vereinbar. Die Strategie 2027 sieht aber auch Enterprise- und OEM-Verträge vor, in denen
Kunden mandari unter anderen Bedingungen erhalten oder in eigene Produkte einbetten. Eine
AGPL-Abhängigkeit im Kern würde solche Verträge an eine kostenpflichtige Lizenz von Artifex binden
oder die Abhängigkeit müsste für diese Ausgaben entfernt werden.

## Entscheidung

- **pypdfium2** (Python-Anbindung an PDFium, die PDF-Engine von Chromium; Apache-2.0 oder
  BSD-3-Clause, PDFium selbst BSD-3-Clause/Apache-2.0) für Seitenobjekte samt Form-XObjects,
  Rasterbilder (Pixelmaße, Filter, Bittiefe), Text mit Rechtecken und das Rendern.
- **pypdf** (BSD-3-Clause, bereits Abhängigkeit) für Einträge des Seitenverzeichnisses, die PDFium
  nicht herausgibt (`/VP`, `/Measure`, `/LGIDict`).
- Die Analyse liegt fachfrei in `apps/common/documents/page_analysis.py` (Plattform-Schicht) und zieht
  mit der einen Texterkennung (#530) nach `shared/`, sobald es dort eine Dokumentbibliothek gibt.
- PyMuPDF wird nicht als Abhängigkeit aufgenommen.

## Alternativen

- **PyMuPDF.** Bequemste API (`get_drawings`, `get_image_rects`, `extract_image`), im Machbarkeitstest
  bewährt. Verworfen wegen der Lizenzbindung für OEM- und Enterprise-Verträge; bleibt Option, falls
  eine kommerzielle Lizenz von Artifex ohnehin erworben wird.
- **pdfminer.six** (MIT) für Text mit Positionen plus pypdfium2 zum Rendern. Langsamer bei großen
  Plänen, zweiter Parser; pypdfium2 liefert Text mit Rechtecken selbst. Verworfen.
- **Poppler (pdf2image, pdftotext).** GPL-Werkzeuge als Unterprozess, keine Seitenobjekte. Für die
  Texterkennung weiter im Einsatz, für die Seitenanalyse ungeeignet.
- **GDAL** (MIT) für GeoPDF. Groß, nur für den seltenen Fall echter GeoPDFs nötig; pypdf erkennt die
  Merkmale bereits. Später prüfen, wenn GeoPDFs häufiger auftreten.

## Folgen

**Positiv**

- Keine Lizenzbindung über die AGPL hinaus; OEM- und Enterprise-Ausgaben bleiben ohne Zusatzlizenz
  möglich.
- PDFium rendert schnell und robust (Chromium-Engine); Wheels bringen die Bibliothek mit, keine
  Systempakete.
- Auf der Stichprobe des Machbarkeitstests liefert die Analyse dieselben Maßstäbe (1:500, 1:5.000,
  1:510, 1:14.300) und Rasterauflösungen (400, 201, 144 dpi) wie PyMuPDF.

**Negativ**

- Vektorpfade sind nur als Segmentzahl und über Einzelaufrufe zugänglich; komplexere Auswertungen
  (Geltungsbereich über Legendensymbole, Koordinatenkreuze) brauchen mehr eigenen Code als mit
  `get_drawings`.
- Zwei Bibliotheken für ein Dokument (PDFium und pypdf); pypdf liest nur die Seitenverzeichnisse.
- PDFium-Aufrufe laufen über ctypes; sehr große Pläne (Hunderttausende Objekte) brauchen einige
  Zehntelsekunden bis Sekunden je Seite. Obergrenzen je Seite und Dokument sind gesetzt.

## Prüfung (Fitnessfunktion)

- `requirements.txt` enthält kein PyMuPDF (`pymupdf`, `fitz`); der Abhängigkeitsbericht (SBOM) nennt
  die Lizenzen.
- Tests der Seitenanalyse (`apps/common/tests/test_page_analysis.py`) laufen ohne Netz auf erzeugten
  PDFs.
