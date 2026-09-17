# Uploads: erlaubte Dateitypen und Größen

BSI IT-Grundschutz verlangt als Basis-Anforderung, Upload-Funktionen nach Dateigröße,
Dateityp und Speicherort einzuschränken (CON.10.A5, APP.3.1.A4). In mandari steht diese
Regel an genau einer Stelle: `apps/common/uploads.py`, Funktion `validate_upload`.
Jede Upload-Stelle ruft sie mit einem Profil und einer Höchstgröße auf (Issue #260).

## Profile

| Profil | Endungen | Einsatz |
|---|---|---|
| `DOCUMENTS` | pdf, doc, docx, odt, rtf, txt, md, xls, xlsx, ods, csv, ppt, pptx, odp, png, jpg, jpeg, gif, webp, zip | Anlagen der Gremien- und Fraktionsarbeit |
| `IMAGES` | png, jpg, jpeg, gif, webp | Dateien, die eingebettet angezeigt werden (Logos, Profilbilder) |
| `PDF` | pdf | Briefköpfe |
| `IMPORTABLE_DOCUMENTS` | pdf, docx | Antrags-Import mit Textübernahme |
| `DATA` | csv, json | Import-Schnittstellen (xml steht in `NEVER` und ist damit ausgeschlossen) |

`NEVER` sticht jedes Profil: HTML, SVG, XML/XSL, JavaScript, Serverskripte und
ausführbare Dateien werden nie angenommen. SVG fehlt auch im Bildprofil bewusst, weil
eine SVG-Datei Skript enthalten kann und vom Browser als aktives Dokument behandelt wird.
Gemessen wird die letzte Endung des Dateinamens; Pfadangaben im Namen ändern nichts.

## Upload-Pfade

| Bereich | Profil | Höchstgröße | Prüfende Stelle |
|---|---|---|---|
| Session: Anlagen zu Vorlagen und Sitzungen | `DOCUMENTS` | `file_service` | `apps/session/services/file_service.py` |
| Work: Anlagen zu Anträgen | `DOCUMENTS` | 50 MB | `MotionDocumentForm.clean_file` |
| Work: Antrags-Import (Textübernahme) | `IMPORTABLE_DOCUMENTS` | 25 MB | `DocumentImportView` |
| Work: Briefköpfe | `PDF` | 10 MB | `LetterheadCreateView`, `LetterheadEditView` |
| Work: Sitzungsvorbereitung | `DOCUMENTS` | 50 MB | `meetings/services.add_document_upload` |
| Work: Fraktionssitzungen | `DOCUMENTS` | 20 MB | `faction/views/panel.py` |
| Work: Aufgaben-Anhänge | `DOCUMENTS` | 20 MB | `TaskAttachmentForm.clean_file` |
| Work: Aufgaben-Import | `DATA` | 5 MB | `TaskImportFileView` |
| Work: Support-Anhänge | `DOCUMENTS` | 10 MB | `support/views.py` (ungeeignete Anlagen werden übergangen) |
| Work: Organisationslogo | `IMAGES` | 5 MB | `organization/services._pruefe_bild` |
| Work: Profilbild | `IMAGES` | 5 MB | `organization/services._pruefe_bild` |

## Auslieferung

`serve_media` liefert alles außer eingebetteten Bildformaten mit
`Content-Disposition: attachment` und `X-Content-Type-Options: nosniff` aus. Ein
hochgeladenes Dokument wird also heruntergeladen, nicht im Browser gerendert; nur
Logos, Profilbilder und ähnliche Bilder werden eingebettet (`is_embeddable`).

## Gate

`scripts/check_upload_validation.py` läuft im Lint-Job und meldet jedes Modul, das
`request.FILES` liest oder ein Datei-Formularfeld deklariert, ohne `validate_upload`
zu benutzen. Wer die Datei an eine prüfende Stelle weiterreicht, trägt das Modul mit
Begründung in `DELEGIERT` ein; die Liste darf nur schrumpfen.
