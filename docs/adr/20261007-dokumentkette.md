# Dokumentkette: Abruf, Ablage und Texterkennung als Aufträge über Ereignisse

- Status: angenommen (Zielbild freigegeben am 07.10.2026, #919)
- Datum: 2026-10-07
- Issue: #919 (Epic #476)
- Bezug: [A1 Schichtenmodell](20260929-schichtenmodell.md), [A2 Ereignistechnik](20260929-ereignistechnik-postgres.md),
  [A4 Aufträge und Zeitpläne](20260929-auftraege-und-zeitplaene.md), [A5 Ereignisverträge](20260929-ereignisvertraege.md),
  [A7 Kanonisches Modell](20260929-kanonisches-modell.md),
  [Schema-Vertrag Django/Ingestor](20260909-schema-contract-django-ingestor.md),
  [Texterkennung als eine Bibliothek](20261004-texterkennung-shared.md) (in zwei Punkten abgelöst),
  `docs/FILE_CACHE.md` (Dokumentablage, Objektspeicher, Löschabgleich #787), `docs/DREHSCHEIBE_UMSTELLUNG.md`

## Kontext

Eine RIS-Datei durchläuft drei Schritte: Inhalt bei der Quelle holen, ablegen, Text erkennen. Heute sind sie an
mehreren Stellen ineinander verwoben:

- **Mehrere Wege zur Quelle.** Der OCR-Worker des Ingestors lädt jede Datei selbst; Dokument-Cache
  (`cache_files`), Vorschau und Auftrag `file.extract_text` laden dieselbe Datei unabhängig davon. Das Ziel aus
  #788, „ein Quellabruf je Datei“, hängt daran, dass die Ablage im OCR-Worker eingehängt ist
  (`INGESTOR_STORES_FILES`); ist sie es nicht, fällt es stillschweigend weg.
- **Mehrere Wege zur Texterkennung.** OCR-Worker des Ingestors (zusätzlich inline im Sync), Auftrag
  `file.extract_text`, Befehl `extract_texts` und der Summarizer, der bei fehlendem Text selbst lädt und erkennt.
- **Der Objektspeicher wird nur von der Vorschau gelesen.** Cache und Auftrag prüfen nur die lokale Platte
  (`file_cache.local_file`); nach dem Verdrängen (`evict_local`) laden sie erneut bei der Quelle.
- **Ein Abruffehler wird zum Erkennungsfehler.** Timeout, 429, 5xx und 404 enden in
  `text_extraction_status = failed`, endgültig und ohne Wiederholung. Liegt das PDF später in der Ablage, setzt
  nichts die Datei zurück: Sie hat ein abgelegtes PDF, aber keinen Text.
- **Wettlauf beim Speichern.** Das Ergebnis der Erkennung wird ohne Bedingung geschrieben; ein inzwischen
  ersetzter Inhalt (`replace_content`) kann von altem Text überschrieben werden.
- **Nachfolger fragen ab.** Suchindex, Verortung und Zusammenfassung erfahren von neuem Text über Abfragen oder
  gar nicht.

Das ADR „Texterkennung als eine Bibliothek“ hat die Erkennung vereinheitlicht, aber festgelegt, dass die Aufrufer
Abruf und Speichern behalten, und den sofortigen Umzug auf Aufträge verworfen. Beide Punkte löst dieses ADR ab;
die Bibliothek `mandari_dokumente` und der Auftrag `file.extract_text` bleiben.

## Entscheidung

### 1. Drei Schritte, je ein Auftrag, verbunden über Ereignisse

```
Ingestor (Eingangsadapter, nur Metadaten)
  └─ ris.file.changed (added | replaced)                         vorhanden
       └─ Abonnement ris.dokumentkette → file.fetch(file_id)
            Auftrag „Abruf“ (Warteschlange fetch)
            beanspruchen, Ausschlüsse prüfen, robots.txt, Drossel je Host, gestreamt, sha256
            → Ablage (lokal, sofort in den Objektspeicher), Blob verknüpfen
            └─ ris.file.content_stored v1 (intern)                neu
                 └─ Abonnement ris.dokumentkette → file.extract_text(file_id)
                      Auftrag „Erkennung“ (Warteschlange ocr, Parallelität 1)
                      beanspruchen, liest nur aus Ablage bzw. Objektspeicher
                      └─ ris.file.text_extracted v1 (intern)      vorhanden
                           └─ Suchindex · Verortung · Zusammenfassung verwerfen
```

- **Ein Abonnement** `ris.dokumentkette` (`hub.ris`, `transactional=True`) behandelt beide Ereignistypen.
  Handler und Einreihen laufen in einer Transaktion; Argumente sind nur Kennungen (A4). Jeder Auftrag meldet sein
  Ergebnis per `publish()` beim Eigentümer `hub.ris` (A4, A5). Das Abonnement verarbeitet nur Ereignisse mit
  Mandant `source:*` (RIS-Bestand) und `change ∈ {added, replaced}`; `renamed` und `removed` werden ignoriert,
  unbekannte Datei-Kennungen übersprungen. Session-Dateien (Mandant `session:*`) haben eine eigene Ablage
  (`SessionFileBlob`) und sind nicht Teil dieser Kette.
- **Ein Weg zur Quelle.** Datei-Downloads liegen in genau einem Modul, `hub.ris.abruf`. Dokument-Cache,
  Löschabgleich (`verify`, `replace_content`) und Vorschau nutzen es; der Ingestor lädt keine Dateien mehr (ab
  Etappe 2). Die Vorschau ruft es synchron auf, wenn ein Inhalt fehlt (Live-Abruf mit Write-Through). Sie schreibt
  damit Anreicherungsspalten außerhalb eines Auftrags: **ausdrückliche Ausnahme zu A7**, begrenzt auf dieses
  Modul, damit Nutzer nicht auf einen Auftrag warten. Der Löschabgleich lädt bewusst erneut, um Änderungen zu
  erkennen; das ist die zweite benannte Ausnahme von „ein Quellabruf je Inhalt“.
- **Ein Weg zur Erkennung.** Nur der Auftrag `file.extract_text` erkennt Text, und er ruft **nie** selbst bei der
  Quelle ab. `extract_texts` plant nur noch Aufträge ein; der Summarizer liest den gespeicherten Text und erkennt
  nicht selbst.
- **Ein Sicherheitsnetz statt zweier Abfragen.** Mit `DOCUMENT_FETCH_SUBSCRIPTION=aktiv` wird der Zeitplan
  `texterkennung_einplanen` zum Zeitplan `dokumentkette_nachholen` (stündlich) und übernimmt die Aufgaben von
  `cache_files`: fällige Wiederholungen des Abrufs, nie abgerufene Inhalte, abgelegte Inhalte ohne Text, Text aus
  einer älteren Erkennungsversion, liegen gebliebene Beanspruchungen. Bis dahin laufen `texterkennung_einplanen`
  (alle 2 Minuten) und `cache_files` weiter, beide über dieselben Funktionen wie oben. Am Ende gibt es eine
  Abfrage über den Bestand, nicht zwei.

### 2. Doppelte Arbeit verhindert die Beanspruchung, nicht der Schlüssel

- **Beanspruchen ist atomar.** Ein Auftrag beginnt damit, die Datei zu beanspruchen: Abruf `local_status` →
  `fetching`, Erkennung `text_extraction_status` → `processing`, jeweils per `SELECT … FOR UPDATE SKIP LOCKED` und
  nur aus einem Zustand, in dem Arbeit ansteht. Misslingt das (schon beansprucht, schon erledigt, ausgeschlossen),
  endet der Auftrag ohne Wirkung. Zwei Aufträge für dieselbe Datei rufen also nie parallel ab. Liegen gebliebene
  Beanspruchungen (Absturz) gibt das Sicherheitsnetz nach der Zeitgrenze der Warteschlange frei, wie heute
  `release_stale_extractions`; bis Etappe 3 tun das `cache_files` und `texterkennung_einplanen` zu Beginn jedes
  Laufs.
- **Zeitpläne und Sicherheitsnetz beanspruchen nicht.** Sie reihen nur Dateien ein, für die noch kein Auftrag
  wartet, höchstens N je Lauf (`TEXT_EXTRACTION_QUEUE_DEPTH`, `DOCUMENT_FETCH_MAX_QUEUED`). Beansprucht wird nur beim
  Start des Auftrags: Abruf aus `none`, `retry` (fällig) oder `error` (fällig), Erkennung aus `pending` oder aus
  `completed` mit älterer Version. Das heutige Vorab-Beanspruchen in `texterkennung_einplanen` entfällt.
- **Vorschau und Löschabgleich beanspruchen nicht.** Steht eine Datei auf `fetching`, zeigt die Vorschau „wird
  geladen“, statt parallel abzurufen; der Löschabgleich überspringt sie in diesem Lauf. Den Zustand vor der
  Beanspruchung hält der Auftrag selbst; endet er ohne Ergebnis (Platte, Objektspeicher, Takt), stellt er ihn
  wieder her.
- **Idempotenzschlüssel nur für Aufträge aus Ereignissen**, damit eine doppelte Zustellung nicht doppelt einreiht:
  `fetch:<file>:<event_id>` bzw. `ocr:<file>:<event_id>`. `enqueue_once` hält einen Schlüssel, solange die
  Auftragszeile aufbewahrt wird (erledigt 14 Tage, tot 90 Tage); ein Schlüssel je Ereignis sperrt deshalb keine
  spätere, gewollte Wiederholung.
- **Wiederholungen, Selbst-Neueinplanung** (Platte voll, Objektspeicher gestört, Takt belegt) **und Sicherheitsnetz
  reihen ohne Schlüssel ein.** Doppelte Einträge in der Warteschlange sind harmlos, weil nur einer beanspruchen kann.

### 3. Was nie abgerufen und nie erkannt wird

- **Aufträge und Sicherheitsnetz** überspringen Dateien, die gelöscht sind (`deleted`, alle Gründe einschließlich
  `datenschutz` und `withdrawn_by_publisher`), gesperrt sind (`file_reconcile.blocked_q()`, #787) oder deren Inhalt
  nach #787 gelöscht wurde (`content_purged_at`). Geprüft wird beim Einplanen und beim Beanspruchen. Ein
  gelöschter Inhalt wird so nie über das Sicherheitsnetz zurückgeholt.
- **Der Löschabgleich** ruft `hub.ris.abruf` für gesperrte Dateien ausdrücklich auf, sonst gäbe es kein
  `restore_reappeared`. Ausgenommen bleiben dort nur `deleted` und `content_purged_at`.
- **Abruf erlaubt** heißt: Die Quelle ist aktiv, `sync_config["file_downloads"]` ist nicht `false`, die Quelle ist
  nicht in Schonung (`file_cache.source_paused`) und robots.txt erlaubt den Abruf. Bei `file_downloads = false`
  entsteht kein Zustand je Datei; sie werden nur übersprungen.

### 4. Getrennte Zustände: Abruf und Erkennung

**Abruf** (`local_status`, neu dazu `fetch_attempts`, `fetch_next_at`, `fetch_error` als Fehlercode):

| Ergebnis | Zustand | Weiter |
|---|---|---|
| beansprucht | `fetching` | – |
| Inhalt abgelegt | `ok` | `content_stored` (siehe 6) |
| Timeout, Verbindung, 429, 5xx, leere Antwort, robots.txt nicht erreichbar | `retry` | nach 15 min, 1 h, 6 h, 24 h, 72 h; danach `error` |
| 404/410 innerhalb von 7 Tagen nach `created_at` (unsere Erfassung) | `retry` | wie oben; die Quelle veröffentlicht das Objekt oft vor der Datei |
| 404/410 später oder nach allen Wiederholungen | `missing` | Löschabgleich übernimmt |
| HTML statt Datei (Bot-Schutz), robots.txt verbietet | `refused` | nach Freigabe (`robots_override` bzw. Befehl `dokumentkette freigeben <quelle>`) zurück auf `none` |
| Größengrenze überschritten | `too_large` | Erkennung `skipped` |
| Platte unter `FILE_CACHE_MIN_FREE_GB` | Zustand vor der Beanspruchung | Auftrag später, kein Versuch gezählt |
| Objektspeicher gestört | Zustand vor der Beanspruchung | Auftrag später, **kein Quellabruf** (siehe 5) |
| `error` | `error` | Sicherheitsnetz versucht es wöchentlich erneut |

- `refused` ist bewusst nicht „gesperrt“: Gesperrt im Sinn von #787 heißt in der Quelle gelöscht oder nicht mehr
  abrufbar (`blocked_q()`); `refused` heißt, die Quelle lässt uns nicht laden.
- Die Wiederholung plant der Auftrag selbst mit `run_after` ein; die Versuche des Aufgaben-Backends
  (`max_attempts`) bleiben für Abstürze des Auftrags. `fetch_attempts` wird bei Erfolg zurückgesetzt.
- Die Vorschau setzt dieselben Zustände (über `hub.ris.abruf`), nicht mehr sofort `missing` bei der ersten 404.
- `source_missing_since` setzt weiterhin nur der Löschabgleich (mit der Bremse `commit_missing`); er nimmt
  `local_status = missing` in seine Auswahl auf, damit auch nie abgelegte Dateien geprüft werden.
- **Datenmigration** (Etappe 1): `error` mit Robots-Präfix und `error` mit „HTML statt Datei“ werden `refused`.
  `requeue_blocked_files`, `pending_queryset`, `source_health` und `cache_stats` lesen die neuen Werte.

**Erkennung** (`text_extraction_status`, neu dazu `text_source_sha256` und `text_extraction_version`):

- Beansprucht werden nur Dateien mit `local_status = ok`. `failed` heißt ausschließlich: Inhalt nicht lesbar oder
  Speichergrenze. **Ein Abruffehler ändert `text_extraction_status` nie.**
- Bestätigt `local_copy` beim Lesen, dass der Inhalt weder lokal noch im Objektspeicher liegt, setzt der Auftrag
  `local_status = none` und die Erkennung zurück auf `pending` und endet; den Abruf übernimmt der Abrufweg.
- `text_source_sha256` ist der `sha256_hash` des Inhalts, aus dem der Text stammt; `text_extraction_version` die
  Version der Bibliothek `mandari_dokumente`. Fehlt bei Altbestand `sha256_hash`, berechnet der Auftrag ihn beim
  Lesen und trägt ihn nach.
- **Bedingtes Schreiben:** Text wird nur gespeichert, wenn der sha256 des Auftrags noch der aktuelle Inhalt ist:
  `UPDATE … WHERE id = %s AND sha256_hash IS NOT DISTINCT FROM %s` (nicht über `blob_id`, das im alten Layout leer
  ist). Sonst verwirft der Auftrag sein Ergebnis; der neuere Inhalt wird eigens erkannt.
- **Neue Erkennungsversion:** Das Sicherheitsnetz plant betroffene Dateien schrittweise neu ein. Der alte Text
  bleibt bis zum bedingten Überschreiben stehen; `completed` darf dafür wieder beansprucht werden.

### 5. Ablage und Objektspeicher

- Gelesen wird über eine Funktion (`file_store.local_copy`): lokal, sonst aus dem Objektspeicher mit Hashprüfung.
- **Nicht vorhanden ist nicht gestört.** `local_copy` und `object_storage.exists` liefern künftig drei Ergebnisse
  (vorhanden, fehlt, gestört); heute ergibt jeder Fehler `None`. `local_status` geht nur bei bestätigtem Fehlen auf
  `none`. Bei einer Störung wird der Auftrag später wiederholt; es gibt keinen Quellabruf. Eine Störung des
  Objektspeichers darf nie massenhafte Abrufe bei den Kommunen auslösen.
- Der Abruf lädt einen neuen Inhalt **sofort** in den Objektspeicher hoch (im selben Auftrag); erst danach darf
  `evict_local` ihn lokal verdrängen. `FILE_CACHE_MIN_FREE_GB` gilt immer. Der stündliche Upload bleibt als
  Nachholer für Altbestand.
- **Abgelegt werden alle Kommunen, deren Abruf erlaubt ist** (Entscheidung 07.10.2026, #919), nicht nur
  gelistete, sobald die Erkennung im Worker läuft (`TEXT_EXTRACTION_RUNNER=worker`). Vorher lädt der Ingestor
  noch selbst, und eine erweiterte Ablage ergäbe doppelte Abrufe. Die lokale Platte ist mit Objektspeicher
  Zwischenspeicher (`OBJ_CACHE_MAX_GB`); ohne Objektspeicher begrenzt `FILE_CACHE_MIN_FREE_GB`, und Abrufe warten,
  statt Text zu verlieren.
- **Stichtag je Quelle:** `sync_config["document_since"]`. Beim Umschalten (Etappe 2) wird er für bestehende, bisher
  nicht abgelegte Quellen auf den Umschaltzeitpunkt gesetzt, für neu angelegte Quellen auf das Ende ihres ersten
  vollständigen Syncs; für die beim Umschalten schon abgelegten (gelisteten) Quellen bleibt er leer. **Ein leerer Stichtag gilt nur für
  diese Quellen:** Eine danach angelegte Quelle bekommt beim Anlegen `document_since = "ausstehend"` und wird nicht
  abgerufen, bis die Anwendung nach dem Abschluss ihres ersten vollständigen Syncs (`quelle_synchronisieren` bzw.
  Sync-Protokoll) den Stichtag setzt; der Ingestor setzt ihn nicht. Dateien mit `created_at` vor
  dem Stichtag sind **Altbestand** und werden nur abgerufen, wenn für die Quelle `sync_config["document_backfill"] =
  true` gesetzt ist; das geschieht je Quelle nach Freigabe (Last bei der Quelle). Ohne Freigabe behalten diese
  Dateien ihren vorhandenen Text. **Neue Dateien** (ab Stichtag) aller erlaubten Quellen werden abgerufen.

### 6. Vertrag `ris.file.content_stored` v1

- Eigentümer `hub.ris`, Sichtbarkeit `intern` (Anreicherung des Bestands, keine Veröffentlichung), Aggregat
  `File`, kanonische Kennung wie `ris.file.changed`.
- Nutzlast: `file` (Pflicht), `sha256` (Pflicht, Muster `^[0-9a-f]{64}$`, als neues Kennungsmuster bewusst in
  `test_schemas.py` eingetragen), `size`, `mime_type` (Muster `^[a-z0-9.+-]+/[a-z0-9.+-]+$`), `change`
  (`stored` | `replaced` | `restored`). Kein Inhalt, keine Adresse der Quelle.
- Gemeldet, wenn sich der Inhalt ändert (neuer sha256) **oder** ein Inhalt abgelegt bzw. wiederhergestellt wird,
  zu dem der Text fehlt (`restore_reappeared`, Nacharbeit, verlorener lokaler Inhalt mit gleichem Hash). Nicht bei
  jedem erfolgreichen Abruf.
- `ris.file.text_extracted` v1 bekommt additiv das optionale Feld `sha256`.
- Wird der Inhalts-Hash später öffentlich (offene Frage O6, Änderungsfeed), ist das ein v2, keine additive
  Änderung.
- Wie alle `ris.*`-Ereignisse hängt es an `INGESTOR_EVENTS_ENABLED` und `sync_config["events_enabled"]`. Quellen
  ohne Ereignisse bleiben im Sicherheitsnetz.

### 7. Schichten

- **Plattform:** fachfreier Inhaltsspeicher `apps.common.blobstore` (Schlüssel `sha256/<ab>/<sha256>`, lokal und
  S3-kompatibel; heute `insight_core/services/object_storage.py` und die Pfadlogik aus `file_store`). Er kennt
  weder `OParlFile`, `OParlFileBlob` noch Kommunen; die Referenzzählung bleibt in `insight_core`. Dazu die
  Bibliothek `mandari_dokumente` (`shared/`). Umzug in Etappe 3, Schlüssel bleiben unverändert.
- **Drehscheibe (`hub.ris`):** Modul `hub.ris.abruf` (Download, Fehlerklassen, Zustände des Abrufs), Logik der
  Aufträge `file.fetch` und `file.extract_text`, Abonnement `ris.dokumentkette`, Vertrag und `publish()`. Der
  registrierte Auftragspfad `insight_core.background_tasks.file_extract_text` bleibt als dünne Hülle, damit wartende
  Aufträge beim Deploy weiterlaufen.
- **RIS-Bestand (`insight_core`):** `OParlFile`, `OParlFileBlob`, Zustandsspalten. Neue Spalten sind nullbar oder
  haben `db_default` und stehen in `ENRICHMENT_FIELDS` des Ingestors (Schema-Vertrag). Anreicherungsspalten
  schreiben nur Aufträge und `hub.ris.abruf` (A7 mit der Ausnahme aus 1).
- **Ingestor:** Eingangsadapter für Metadaten; meldet `ris.file.changed` wie bisher und lädt ab Etappe 2 keine
  Dateien mehr.
- **import-linter:** Der Download (heute `document_extraction.download_to_file` und der Abruf in
  `file_cache.fetch_and_cache` über `safe_fetch`) zieht nach `hub.ris.abruf`. Ein Vertrag erlaubt den Import nur aus
  `hub.ris.*`, `insight_core.services.file_cache`, `insight_core.services.file_reconcile` und
  `insight_core.views.files` (Vorschau); die Admin-Aktion „Dokumente laden“ geht über `file_cache`. Der Summarizer
  lädt heute mit eigenem HTTP-Code; dafür gilt ein eigener Test (`insight_ai` lädt keine Dateien).

### 8. Last und Fairness

- Warteschlange **`fetch`** im Worker (neu in `TASK_QUEUES` und im `--queues` des Workers; ergänzt die Liste der
  Warteschlangen in der Spezifikation), Parallelität 2. Abrufe sind gestreamt und brauchen wenig Speicher.
- Vor jedem Versuch gilt die Quellen-Schonung (Fehlschläge in Folge, zurückgestellte Quellen); bei einer
  Sperre der ganzen Quelle wartet die Quelle, nicht jede Datei einzeln.
- Die Drossel je Host (`request_interval` der Quelle) gilt prozessübergreifend und teilt sich den Takt mit dem
  Metadaten-Sync. Ist der Takt belegt, plant sich der Auftrag mit `run_after` neu ein, statt zu warten.
- Je Quelle höchstens `DOCUMENT_FETCH_MAX_QUEUED` (Standard 200) wartende Abrufe; der Rest kommt über das
  Sicherheitsnetz. Neue Dateien haben Vorrang vor dem Nachholen von Altbestand (Priorität).
- Warteschlange `ocr` im eigenen Runner (`worker-heavy`) mit Ablage und Objektspeicher, Parallelität 1.
  **Abweichung von Spezifikation N8 (OCR-Warteschlange höchstens 1 GB):** Tesseract braucht je Seite bis
  `OCR_MEMORY_LIMIT_MB` (in Produktion 2 GB); der Runner bekommt eine eigene Grenze (Richtwert 3 GB), die
  Seitengrenzen aus #817 bleiben. Die Spezifikation N8 wird mit diesem ADR angepasst. Damit ist O12
  (Texterkennung in eigenem Prozess) beantwortet.

### 9. Nachfolger

- Suchindex: Abonnement `suchindex` (#821) wertet `text_extracted` aus (heute im Schatten).
- Verortung: plant bei `text_extracted` die Verortung der Datei ein, statt alle 15 Minuten abzufragen.
- Zusammenfassung: **verwerfen, nicht neu erzeugen.** Neuer Text markiert eine bestehende Zusammenfassung als
  veraltet; erzeugt wird sie wie heute erst auf Abruf. Fehlt der Text, zeigt der Summarizer einen Hinweis und
  plant die Erkennung mit Vorrang ein, statt selbst zu laden.

### 10. Kennzahlen und Prüfung

- Metriken: `mandari_files_fetch_queued`, `mandari_files_fetch_retry_due`, `mandari_files_fetch_errors_total`
  (je Quelle und Fehlercode), `mandari_files_stored_without_text`, `mandari_files_text_outdated`.
- `/health/worker/` meldet Warnung, wenn fällige Wiederholungen länger als 6 h liegen oder abgelegte Inhalte ohne
  Text länger als 24 h warten (Schwellen per Einstellung).
- Umstellung nach der Checkliste in `docs/DREHSCHEIBE_UMSTELLUNG.md` mit Abbruchkriterien: Fehlerquote des
  Abrufs je Quelle höher als vor dem Umschalten, Quellabrufe je Inhalt > 1 (außer Löschabgleich), wachsender
  Rückstau.

### 11. Einführung und Rückweg

**Voraussetzungen:** `TASKS_BACKEND=journal` mit laufendem Runner für `default`, `fetch` und (im `worker-heavy`)
`ocr`. `TEXT_EXTRACTION_RUNNER=worker` und `DOCUMENT_FETCH_SUBSCRIPTION` (`schatten`/`aktiv`) verweigern den Start
ohne diese Voraussetzung (`ImproperlyConfigured`), wie `WORK_NOTIFICATION_SUBSCRIPTION`.

1. **Etappe 1 – Fundament** (mit Etappe 2 ausgeliefert, wirkt aber auch ohne Umschalten):
   - Modul `hub.ris.abruf` mit Fehlerklassen, Zuständen und Beanspruchung; `cache_files`, Vorschau und
     Löschabgleich nutzen es.
   - Auftrag `file.extract_text` liest über `local_copy`, ruft nie selbst ab, schreibt bedingt; Beanspruchung wie
     in 2.
   - Neue Spalten, Datenmigration (`refused`), dreiwertiges `exists`.
   - Befehl `dokumentkette zuruecksetzen` (idempotent, im Rückfallabschnitt von `DEPLOYMENT.md`): setzt `retry` und
     `fetching` auf `none` und `refused` je nach `fetch_error` zurück auf `error` mit Robots-Präfix bzw. mit dem
     Text „HTML statt Datei“, damit ein älteres Image sie wieder findet.
   - Befehl `dokumentkette nacharbeiten` (Standard Probelauf). Er wählt Dateien mit
     `text_extraction_status = failed` und einem Fehlertext, der mit „Download“ beginnt („Download failed: …“ und
     „Download fehlgeschlagen“): mit Inhalt in Ablage oder Objektspeicher → Erkennung `pending`; ohne → Abruf `none`
     und Erkennung `pending`, außer bei nicht freigegebenem Altbestand (siehe 5): Der bleibt ohne Inhalt
     unverändert. **Ausführung in Produktion erst, wenn Etappe 2 läuft** (sonst lädt der Ingestor
     erneut bei der Quelle), und nur nach Freigabe.
2. **Etappe 2 – Umschalten der Erkennung:** `TEXT_EXTRACTION_RUNNER=worker` mit `worker-heavy`; die Texterkennung
   im Ingestor (Daemon und inline im Sync) ruht, `INGESTOR_STORES_FILES` hat keine Wirkung mehr. Ab jetzt Ablage
   für alle Kommunen mit erlaubtem Abruf; der Umschaltbefehl setzt `document_since` (siehe 5), Altbestand je Quelle
   nach Freigabe. **Rückweg:**
   `TEXT_EXTRACTION_RUNNER=ingestor`.
3. **Etappe 3 – Ereigniskette:** Vertrag, Auftrag `file.fetch`, Abonnement `ris.dokumentkette`, Umzug des
   Inhaltsspeichers nach `apps.common.blobstore`. Schalter `DOCUMENT_FETCH_SUBSCRIPTION` (`aus` = Zeitpläne wie
   bisher, Standard; `schatten` = das Abonnement zählt nur, was es einplanen würde, Zustand über
   `events_subscription.state` und `delivery.shadow`; `aktiv` = das Abonnement plant ein, `dokumentkette_nachholen`
   ersetzt `cache_files` und `texterkennung_einplanen`). **Rückweg:** Schalter auf `aus`.
4. **Etappe 4 – Nachfolger und Aufräumen:** Suchindex, Verortung, Zusammenfassung wie in 9. **Aufräumen im
   Folge-Release** (Spezifikation §7 Nr. 4): Extractor und `extract-daemon` im Ingestor, `INGESTOR_STORES_FILES`,
   `TEXT_EXTRACTION_RUNNER`, Zeitpläne `cache_files` und `texterkennung_einplanen`.

Migrationen nach Erweitern → Umstellen → Aufräumen. Kein Datenverlust: Texte, Inhalte und Zustände bleiben
erhalten; zurückgesetzt werden nur Zustandsspalten.

## Alternativen

- **Den Ingestor-Extractor reparieren** (Ablage einhängen, Abruffehler wiederholen, zuerst in der Ablage
  nachsehen). Kleinster Eingriff, aber der Ingestor schreibt weiter Anreicherungsspalten am Eigentümer vorbei,
  hat keinen Zugang zum Objektspeicher und koppelt Abruf und Erkennung. Verworfen.
- **Nur Zeitpläne, keine Ereignisse.** Einfach, aber Abfragen über den ganzen Bestand, Verzögerung bis zum nächsten
  Lauf, und Nachfolger erfahren nichts. Bleibt als Sicherheitsnetz.
- **Abruf und Erkennung in einem Auftrag.** Ein Abruffehler wäre wieder ein Erkennungsfehler, und eine neue
  Erkennungsversion müsste die Quellen erneut belasten. Verworfen.
- **Idempotenz allein über `enqueue_once`.** Schlüssel gelten Tage bis Wochen und würden gewollte Wiederholungen
  verschlucken. Verworfen zugunsten der Beanspruchung.
- **Ereignis bei jedem Abruf.** Mehr Ereignisse ohne neue Information. Verworfen.
- **Warteschlange `adapter` mitnutzen.** Kein neuer Name, aber Abrufe stünden hinter stundenlangen Syncs. Verworfen.

## Folgen

**Positiv**

- Ein Quellabruf je Inhalt (außer Löschabgleich); die Erkennung fasst die Quelle nie an.
- Abruffehler verderben keinen Text mehr; Wiederholungen sind geregelt und sichtbar.
- Neue Erkennungsversionen, Seitenanalyse und KI-Auswertungen laufen über den ganzen Bestand aus der Ablage.
- Nachfolger reagieren auf Ereignisse statt auf Abfragen.
- Vier Erkennungs- und fünf Abrufwege werden je einer; zwei Bestandsabfragen werden eine.

**Negativ**

- Ein Abonnement, ein Vertrag und eine Warteschlange mehr; der Ablauf ist über Ereignisse verteilt und braucht die
  Kennzahlen aus 10.
- Der Objektspeicher wächst mit allen abrufbaren Kommunen.
- Bis zum Aufräumen gibt es Schalter mit zwei Wegen.
- `TASKS_BACKEND=journal` wird für die Texterkennung Pflicht.

## Prüfung (Fitnessfunktion)

- import-linter: `hub.ris.abruf` wird nur aus den in 7 genannten Modulen importiert; Test: `insight_ai` lädt keine
  Dateien.
- Der Auftrag `file.extract_text` öffnet keine Verbindung zur Quelle (Test mit gesperrtem Netz, Inhalt nur im
  Objektspeicher-Ersatz; fehlt der Inhalt, setzt er nur Zustände zurück).
- Ein Abruffehler ändert `text_extraction_status` nicht (Test je Fehlerklasse der Tabelle in 4).
- Gestörter Objektspeicher löst keinen Quellabruf aus.
- Gesperrte, gelöschte und nach #787 geleerte Dateien werden von Aufträgen und Sicherheitsnetz weder eingeplant
  noch abgerufen noch erkannt; der Löschabgleich prüft gesperrte Dateien weiter.
- Zwei Aufträge für dieselbe Datei: nur einer beansprucht, nur ein Quellabruf.
- `replaced` innerhalb von 14 Tagen, Wiederholung und Neueinplanung nach Störung führen jeweils zu einem neuen Lauf.
- Bedingtes Schreiben: Ergebnis zu veraltetem sha256 wird verworfen; Altbestand ohne `blob_id` wird geschrieben.
- Doppelzustellungstest für `ris.dokumentkette`; Abonnement ignoriert Session-Dateien und `renamed`/`removed`;
  ohne Ereignisse arbeitet das Sicherheitsnetz.
- Vertragstest für `ris.file.content_stored` v1 und die Erweiterung von `ris.file.text_extracted`.
- Ingestor: nach dem Aufräumen kein Schreiben von Anreicherungsspalten (Vertragstest gegen `ENRICHMENT_FIELDS`).
