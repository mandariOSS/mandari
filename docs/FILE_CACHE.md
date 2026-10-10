# Lokaler Dokument-Cache (Insight)

Alle OParl-Dateien (Vorlagen, Anlagen, Niederschriften – praktisch nur PDFs) werden lokal
zwischengespeichert. Vorschau und Download kommen dann von der Platte, unabhängig davon, ob das
Ratsinformationssystem gerade erreichbar ist (Issue #87). Der Proxy hält keine Worker mehr
minutenlang fest (Issue #86): kurze Timeouts, lokale Datei zuerst.

Bei der Quelle abgerufen wird nur über ein Modul, `hub/ris/abruf.py` (Issue #919,
`docs/adr/20261007-dokumentkette.md`): Dokument-Cache, Vorschau und Löschabgleich nutzen es, mit denselben
[Zuständen des Abrufs](#zustände-des-abrufs) und Wiederholungen. Ein import-linter-Vertrag (`abruf-ein-weg`)
erlaubt den Import nur aus der Drehscheibe, `file_cache`, `file_reconcile` und der Vorschau.

## Speicherlayout

Seit Issue #788 liegen Dokumente **nach ihrem SHA-256** in der Ablage (`FILE_STORE_LAYOUT=sha256`, Standard):

```
<OPARL_FILES_ROOT>/sha256/<ab>/<sha256>      Inhalt, ohne Dateiendung
<OPARL_FILES_ROOT>/sha256/tmp/*.part         Downloads, die gerade entstehen
```

Details unter [Ablage nach SHA-256](#ablage-nach-sha-256). Das bisherige Layout je Kommune gilt weiter für Kopien,
die noch nicht umgestellt sind, und für Installationen mit `FILE_STORE_LAYOUT=kommune`:

```
<OPARL_FILES_ROOT>/<kommune>/<jahr>/<datei-id>.pdf
```

Je Kommune ein Verzeichnis (Slug, sonst aus dem Namen abgeleitet: `stadt-koeln`, `stadt-muenster`,
`bundesstadt-bonn` …). Dadurch lässt sich pro Stadt eine eigene Storage Box unter genau diesem
Pfad mounten – oder eine große Box für alle.

Der Name wird je Kommune **einmal festgeschrieben** (Feld „Verzeichnis im Dokument-Cache“ im Admin,
`OParlBody.file_cache_dir`): beim ersten Ablegen einer Datei, für den Bestand durch die Migration
`insight_core.0039`. Ein später gesetzter Slug oder ein im RIS geänderter Kurzname verschiebt daher
nichts – vorhandene Kopien (`OParlFile.local_path`, absolute Pfade) bleiben gültig, neue Dateien
landen im selben Verzeichnis, gemountete Storage Boxen bleiben zuständig (Issue #373).

`prune_file_cache --unlisted` räumt den Bestand ausgeblendeter Kommunen ab. Ein Verzeichnis bleibt
stehen, sobald es auch eine gelistete Kommune nutzt – nach ihrem Verzeichnisnamen oder weil dort
Dateien von ihr liegen.

**Rückfall auf ein älteres Image:** Images vor `insight_core.0039` kennen den festgeschriebenen Namen
nicht und leiten das Verzeichnis wieder aus Slug bzw. Kurznamen ab. Weicht der Slug einer Kommune vom
Cache-Verzeichnis ab, landen neue Downloads dann in einem zweiten Verzeichnis (vorhandene Kopien bleiben
über `local_path` gültig), und `prune_file_cache --unlisted` darf mit einem solchen Image nicht laufen.
Deshalb den Slug, wo er als Adresse passt, gleich dem Cache-Verzeichnis wählen – `set_body_slugs` zeigt
Abweichungen an.

## Speicherbedarf (Stand September 2026)

| Kommune | Dateien | Ø Größe | Bestand | Zuwachs/Jahr |
|---------|--------:|--------:|--------:|-------------:|
| Stadt Köln | 94 400 | ~0,38 MB (Stichprobe n=8, RIS war down) | ~35 GB (15–65 GB) | ~10 000 Dateien ≈ 4 GB |
| Stadt Münster | 59 300 | 0,23 MB (Stichprobe n=78) | ~13 GB | ~3 000 Dateien ≈ 0,7 GB |
| Bundesstadt Bonn | 23 900 | 0,68 MB (OParl-Angabe, exakt) | 15,5 GB | unbekannt (Quelle steht seit Februar) |
| Bezirksregierung Köln | 2 000 | ~0,25 MB (Stichprobe n=5) | ~0,5 GB | ~400 Dateien ≈ 0,1 GB |
| **Summe** | **179 700** | | **~65 GB** | **~5–8 GB** |

Stichproben per HEAD-Request am 08.09.2026 (Median liegt deutlich unter dem Durchschnitt: einzelne
große Anlagen dominieren). Das Systemlaufwerk (75 GB, ~46 GB frei) reicht für den Vollbestand
nicht sicher – daher Festplatten-Schutz und Storage Box. Eine 1-TB-Box deckt alle Kommunen für
Jahre ab; die Aufteilung je Stadt ist über das Verzeichnislayout jederzeit möglich.

## Betrieb

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `OPARL_FILES_ROOT` | `<MEDIA_ROOT>/oparl_files` | Wurzelverzeichnis des Caches (Container: `/app/files`) |
| `FILE_CACHE_MAX_MB` | 80 | Größere Dateien werden nicht gecacht, aber weiter durchgereicht |
| `FILE_CACHE_MIN_FREE_GB` | 15 | Unter dieser Grenze wird nichts mehr geschrieben (Schutz des Systemlaufwerks) |
| `FILE_CACHE_MAX_TOTAL_GB` | 0 | Obergrenze der Gesamtgröße in GB, 0 = unbegrenzt (siehe [Obergrenze der Gesamtgröße](#obergrenze-der-gesamtgröße)) |
| `FILE_CACHE_EVICT_TARGET_PERCENT` | 90 | Über der Obergrenze wird bis zu diesem Anteil der Grenze verdrängt (50 bis 100) |
| `FILE_PROXY_TIMEOUT_SECONDS` | 15 | Lese-Timeout des Proxys für Live-Abrufe |
| `RIS_REQUEST_INTERVAL` | 1.0 | Drossel je Host: Mindestabstand in Sekunden zwischen zwei Anfragen an dasselbe RIS, gemeinsam mit dem Ingestor über Redis (je Quelle: `sync_config.request_interval`, 0 = aus) |
| `FILE_PROXY_PACE_MAX_WAIT_SECONDS` | 5 | So lange warten Vorschau und KI-Zusammenfassung höchstens auf ihren Zeitpunkt (samt Abruf einer noch nicht zwischengespeicherten robots.txt), sonst HTTP 503 mit `Retry-After` bzw. die Bitte um einen neuen Versuch. Gewartet wird mit belegtem Abrufplatz (`FILE_PROXY_MAX_CONCURRENT`) und ohne gehaltene Datenbankverbindung |
| `INSIGHT_SOURCE_BACKOFF_FAILURES` | 3 | Ab so vielen Sync-Fehlversuchen in Folge werden Cache-Nachladen und Live-Abruf für die Quelle pausiert |
| `FILE_STORE_LAYOUT` | `sha256` | Ablage nach SHA-256 mit Referenzzählung; `kommune` = bisheriges Layout je Kommune |
| `INGESTOR_STORES_FILES` | `false` (Compose: `true`) | Der Ingestor legt Dateien selbst ab; `cache_files` holt Dateien in der Texterkennung nicht nach. Mit `TEXT_EXTRACTION_RUNNER=worker` ohne Wirkung |
| `DOCUMENT_FETCH_STALE_MINUTES` | 30 | So lange gilt die Beanspruchung einer Datei durch einen Abruf; danach gibt der nächste Lauf von `cache_files` sie frei |
| `DOCUMENT_FETCH_MAX_QUEUED` | 200 | Höchstens so viele Abrufe je Quelle und Lauf von `cache_files`; der Rest kommt im nächsten Lauf |
| `DOCUMENT_FETCH_RETRY_ALERT_HOURS` | 6 | Prüfung `dokumentabruf` in `/health/worker/`: rot, wenn fällige Wiederholungen länger liegen |
| `OBJ_ENABLED` | `false` | S3-kompatibler Objektspeicher (siehe [Objektspeicher](#objektspeicher)) |
| `OBJ_CACHE_MAX_GB` | 60 | Größe des lokalen Zwischenspeichers bei eingeschaltetem Objektspeicher |
| `FILE_PURGE_AFTER_DAYS` | 30 | Kopie und Text gesperrter Dokumente nach so vielen Tagen löschen (Löschabgleich) |
| `FILE_PURGE_CONFIRM_GRACE_DAYS` | 7 | Lässt sich die Quelle vor dem Löschen nicht befragen, wartet das Löschen höchstens so viele Tage zusätzlich |
| `FILE_RECONCILE_MAX_MISSING` | 10 | Bremse des Löschabgleichs: Liefern in einem Lauf mehr Dokumente einer Quelle neu `404`/`410`, wird keines gesperrt |
| `FILE_ACCEL_REDIRECT` | `false` | Lokale Kopien liefert der Webserver aus statt Django (siehe [Auslieferung über den Webserver](#auslieferung-über-den-webserver)) |

Nachgeladen wird stündlich um :40 vom Zeitplan `befehl:cache_files` im Worker
(`cache_files --limit 400`, Zeitgrenze 50 Minuten; `DEPLOYMENT.md`, „Geplante Aufgaben“). Ein
Host-Cron ist dafür nicht mehr nötig; ein alter Eintrag überspringt, solange der Worker den Zeitplan
bedient, und gehört aus der Crontab entfernt (Upgrade-Hinweis dort). Abschalten:
`EVENTS_SCHEDULES_DISABLED=befehl:cache_files`.

- Der Zeitplan gibt zuerst liegen gebliebene Beanspruchungen frei, lädt dann fällige Wiederholungen und die
  neuesten Dokumente zuerst nach (je Quelle höchstens `DOCUMENT_FETCH_MAX_QUEUED`); jeder Live-Abruf über die
  Vorschau legt die Datei ebenfalls ab (Write-Through). Die Ausgabe steht im Protokoll des Workers.
- Abgelegt werden gelistete Kommunen, mit `TEXT_EXTRACTION_RUNNER=worker` auch alle übrigen Quellen mit erlaubtem
  Abruf ab ihrem Stichtag ([Ablage für alle Quellen](#ablage-für-alle-quellen-stichtag)).
- `cache_files --stats` zeigt Abdeckung, Belegung, freien Speicher und die Zustände des Abrufs (läuft immer);
  der Betriebsmonitor hat dafür den Check „Dokument-Cache“ und unter „Handlungsbedarf“ Dateien mit Fehler, in
  Wiederholung und mit verweigertem Abruf.
- `purge_deleted` entfernt lokale Kopien getilgter Dateien.
- **Größe:** `local_size` ist die gemessene Größe unserer Kopie (Bytes, `bigint`). `size` bleibt die Angabe
  der Quelle aus OParl; liefert die Quelle keine, überschreibt der Abgleich eine vorhandene nicht mehr mit
  einem leeren Wert. Für Kopien von vor dieser Spalte einmalig `cache_files --sizes` ausführen
  (wiederholbar, liest nur die Dateigröße von der Platte). Die Belegung in `cache_files --stats` stammt
  aus `local_size`; Kopien ohne gemessene Größe weist die Ausgabe gesondert aus.
- **Zugriffsprotokoll:** Jeder Abruf über die Dateivorschau zählt einmal in `oparl_file_access_days`: je Tag,
  Kommune, Ergebnis (Treffer aus der lokalen Kopie, Abruf bei der Quelle, nicht ausgeliefert, gesperrt) und
  Altersklasse des Dokuments (< 30 Tage, < 1 Jahr, < 3 Jahre, älter). Es gibt nur Zähler, keine Adressen,
  Kennungen oder einzelnen Dokumente. `cache_files --stats` zeigt die letzten 30 Tage mit Trefferquote; daraus
  ergibt sich, wie groß ein Zwischenspeicher sein muss. Ein Fehler beim Zählen verhindert die Auslieferung nie.
  Getrennt davon vermerkt jedes ausgelieferte Dokument, wann es zuletzt ausgeliefert wurde
  (`OParlFile.local_accessed_at`, höchstens einmal je Stunde, nur der Zeitpunkt): Danach richtet sich die
  [Obergrenze der Gesamtgröße](#obergrenze-der-gesamtgröße). Erkennbare Crawler und Abrufprogramme (`bot`,
  `crawler`, `spider` im User-Agent, etwa GPTBot, ClaudeBot, Amazonbot, Bytespider, Googlebot, bingbot, dazu
  `python-requests`, `curl` u. Ä.) zählen im Zugriffsprotokoll weiter mit, vermerken aber keine Nutzung: Sie sollen
  nicht bestimmen, was im Cache bleibt.
  Mit der Auslieferung über den Webserver laden PDF-Betrachter große Dokumente in Teilen (Range-Anfragen),
  und jede Anfrage läuft durch Django. Gezählt wird nur die erste Anfrage eines Abrufs (ohne `Range` oder mit
  einem Bereich ab Byte 0); Folgeanfragen zählen weder als Abruf noch mit ihrer Größe.

### Zustände des Abrufs

Jede Datei hat einen Zustand des Abrufs (`local_status`, Issue #919). Ein Abruf beansprucht seine Datei zu Beginn
atomar (`fetching`, `SELECT … FOR UPDATE SKIP LOCKED`); zwei Abrufe derselben Datei fragen die Quelle also nie
doppelt an. Zeitpläne wählen nur aus. Vorschau und Löschabgleich beanspruchen nicht: Läuft ein Abruf, zeigt die
Vorschau „wird geladen“ (HTTP 503, `Retry-After: 15`), der Löschabgleich überspringt die Datei in diesem Lauf.

| Ergebnis | Zustand | Weiter |
|---|---|---|
| Inhalt abgelegt | `ok` | Zähler zurück, im selben Abruf in den Objektspeicher |
| Zeitüberschreitung, Verbindung, 429, 5xx, leere Antwort, robots.txt nicht erreichbar | `retry` | nach 15 min, 1 h, 6 h, 24 h, 72 h; danach `error` |
| 404/410 in den ersten sieben Tagen nach unserer Erfassung | `retry` | wie oben; die Quelle veröffentlicht das Objekt oft vor der Datei |
| 404/410 später oder nach allen Wiederholungen | `missing` | Löschabgleich übernimmt |
| HTML-Seite statt der Datei (Bot-Schutz), robots.txt verbietet | `refused` | erst nach Freigabe (`robots_override` bzw. `dokumentkette freigeben`) |
| größer als `FILE_CACHE_MAX_MB` | `too_large` | – |
| sonstige Fehler (etwa 403, nicht öffentliches Ziel) | `error` | neuer Versuch nach einer Woche |
| Platte unter `FILE_CACHE_MIN_FREE_GB`, Ablage gestört, Takt belegt, Quelle in Schonung | unverändert | kein Versuch gezählt, kein Abruf bei der Quelle |

- **Felder:** `fetch_attempts` (Fehlschläge in Folge), `fetch_next_at` (nächster Versuch; bei `fetching` das Ende
  der Beanspruchung, `DOCUMENT_FETCH_STALE_MINUTES`), `fetch_error` (Fehlercode, etwa `http_5xx`, `html`,
  `robots`), dazu wie bisher der lesbare Grund in `local_error`. Ein Abruffehler ändert die Texterkennung
  (`text_extraction_status`) nie.
- **Nie abgerufen** werden gelöschte (alle Gründe), gesperrte (#787) und nach #787 geleerte Dateien – weder vom
  Zeitplan noch beim Beanspruchen. Der Löschabgleich ruft gesperrte Dateien dagegen ausdrücklich ab.
- **Abruf erlaubt** heißt: Quelle aktiv, `sync_config["file_downloads"]` nicht `false`, keine Schonung, robots.txt
  erlaubt. Bei abgeschaltetem Dateiabruf entsteht kein Zustand je Datei.
- **Fehler** ohne Termin (`error` aus der Zeit vor Issue #919) versucht nur `cache_files --retry-errors` erneut;
  die Admin-Aktion „Lokal zwischenspeichern“ versucht wie bisher jede Datei ohne Kopie.
- **Verdrängt** (`evicted`, [Obergrenze der Gesamtgröße](#obergrenze-der-gesamtgröße), nur ohne Objektspeicher) ist
  kein Zustand, aus dem ein Abruf von selbst beansprucht; weder Zeitplan noch Kennzahlen zählen ihn als wartend.
  Ausdrücklich holen ihn die Vorschau (Live-Abruf, Fehlschläge nach der Tabelle oben wie bei `none`),
  `cache_files --verdraengte` und die Admin-Aktion. Ein bestätigtes Fehlen der Kopie setzt ihn nie auf `none`, und
  `dokumentkette zuruecksetzen` lässt ihn stehen.
- **Kennzahlen:** `mandari_files_fetch_queued`, `mandari_files_fetch_retry_due` und
  `mandari_files_fetch_errors_total` je Quelle bzw. Fehlercode, Prüfung `dokumentabruf` in `/health/worker/`
  (`docs/MONITORING.md`).
- **Befehl `dokumentkette`:** `freigeben <quelle> [--code html|robots]` reiht verweigerte Abrufe einer Quelle neu
  ein; `zuruecksetzen` bereitet einen Rückfall auf ein älteres Image vor (siehe unten); `umschalten` setzt die
  Stichtage der Ablage ([Ablage für alle Quellen](#ablage-für-alle-quellen-stichtag)).
- **Rückfall auf ein älteres Image** (ohne Rückbau der Migration `insight_core.0057`): vorher
  `python manage.py dokumentkette zuruecksetzen` ausführen. Es setzt `retry` und `fetching` auf `none` und `refused`
  zurück auf `error` mit dem Fehlertext, an dem ein älteres Image die Sperre erkennt (robots.txt-Präfix bzw. „HTML
  statt Datei“); idempotent, es ändern sich nur Zustandsspalten. Die Migration `insight_core.0057` hat denselben
  Rückweg.

### Ablage für alle Quellen (Stichtag)

Bis zur Umstellung der Texterkennung auf den Worker (`TEXT_EXTRACTION_RUNNER=worker`) legt der Dokument-Cache nur
gelistete Kommunen ab; der Ingestor lädt Dateien für den Text noch selbst, eine weitere Ablage ergäbe doppelte
Abrufe. Mit `TEXT_EXTRACTION_RUNNER=worker` werden alle Quellen mit erlaubtem Abruf abgelegt (Entscheidung zu
Issue #919), aber nur Dateien ab dem Stichtag der Quelle:

- `sync_config["document_since"]`: leer bei den bisher abgelegten Quellen (mit gelisteter Kommune; alle Dateien),
  sonst ein Zeitpunkt (ISO 8601) oder `ausstehend` (erster vollständiger Sync noch nicht beendet: nichts abrufen).
  Ein leerer Stichtag gilt nur für Quellen mit gelisteter Kommune.
- `sync_config["document_backfill"] = true` gibt den Altbestand (Dateien vor dem Stichtag) einer Quelle frei; das
  geschieht je Quelle nach Freigabe (Last bei der Quelle). Ohne Freigabe behalten diese Dateien ihren Text.
- Die Stichtage setzt die Anwendung, nie der Ingestor: Mit `TEXT_EXTRACTION_RUNNER=worker` trägt jeder Lauf von
  `cache_files` sie nach. Quellen ohne gelistete Kommune und ohne Stichtag bekommen den Zeitpunkt des Laufs (beim
  Umschalten also den Umschaltzeitpunkt), solange ihr erster vollständiger Sync fehlt `ausstehend`; aus
  `ausstehend` wird nach dem ersten vollständigen Sync (`last_successful_full_sync`) dessen Zeitpunkt. Von Hand
  (etwa vor dem Umschalten): `python manage.py dokumentkette umschalten` (mit `--probelauf` nur Anzeige).
- `prune_file_cache --unlisted` lässt ausgeblendete Kommunen stehen, die ablegen.
- Die lokale Platte ist mit Objektspeicher Zwischenspeicher (`OBJ_CACHE_MAX_GB`); ohne Objektspeicher begrenzt
  `FILE_CACHE_MIN_FREE_GB`, und Abrufe warten, statt Text zu verlieren.

### Auslieferung über den Webserver

Ohne weitere Einstellung streamt Django jede lokale Kopie selbst (`FileResponse`): ohne Range-Anfragen,
ohne `ETag`, und jeder Download belegt einen Anwendungs-Thread. Mit `FILE_ACCEL_REDIRECT=true` prüft
Django nur Zugriff und Sperre und antwortet ohne Dateiinhalt mit einer internen Weiterleitung
(`X-Accel-Redirect: /_mandari/dateien/<pfad unterhalb der Ablage>`). Caddy liefert die Bytes aus der
Ablage: Range-Anfragen bekommen `206`, dazu `ETag`, `Last-Modified` und `304` auf bedingte Anfragen.
PDF-Betrachter im Browser laden so zuerst nur die Teile, die sie für die erste Seite brauchen.

Der Block steht im `Caddyfile` (`handle_response` im `reverse_proxy` der Anwendung):

```caddyfile
@dokument header X-Accel-Redirect /_mandari/dateien/*
handle_response @dokument {
	root * {$OPARL_FILES_MOUNT:/srv/mandari-files}
	copy_response_headers {
		include Content-Type Content-Disposition X-Content-Type-Options Content-Security-Policy Cache-Control X-Robots-Tag X-Mandari-Cache X-Request-ID
	}
	rewrite * {rp.header.X-Accel-Redirect}
	uri strip_prefix /_mandari/dateien
	file_server
}
```

- **Schutzkopfzeilen:** Typ, Anzeigeart und Dateiname setzt weiter `file_delivery` in Django (nur passive
  Formate im Browser, alles andere als `application/octet-stream` zum Herunterladen, `nosniff`, Sandbox außer
  bei PDF). Caddy übernimmt genau diese Kopfzeilen; `file_server` bestimmt den Typ dann nicht nach der
  Dateiendung. Fehlte die Übernahme, käme eine HTML- oder SVG-Anlage mit ihrem eigenen Typ im Ursprung
  von Insight, Work und Session an.
- **Nicht in Suchmaschinen:** Auch `X-Robots-Tag: noindex` (Issue #914) kommt aus der Antwort von Django; ohne
  die Übernahme stünden Dokumente aus der Ablage ohne diese Kopfzeile im Netz.
- **Nur unterhalb der Ablage:** Django leitet nur Dateien weiter, die nach Auflösen aller Verweise unterhalb
  von `OPARL_FILES_ROOT` liegen und deren Pfadteile nur aus Buchstaben, Ziffern, `.`, `_` und `-` bestehen
  (keine versteckten Dateien, kein `..`). Alles andere liefert Django wie bisher selbst aus.
- **Nur Antworten von Django:** Caddy wertet `X-Accel-Redirect` ausschließlich in der Antwort der Anwendung
  aus. Eine von außen mitgeschickte Kopfzeile bewirkt nichts, der Pfad `/_mandari/dateien/` ist von außen
  nicht erreichbar.
- **Voraussetzungen:** Caddy liest die Ablage nur lesend unter `OPARL_FILES_MOUNT` (Compose: Volume
  `mandari_files` unter `/srv/mandari-files:ro`). Der relative Pfad ist in beiden Containern derselbe. Erst
  danach `FILE_ACCEL_REDIRECT=true` setzen und die Anwendung neu starten; ohne den Block im Caddyfile kämen
  leere Antworten an. Zurück: Schalter auf `false`, Neustart.
- **Prüfen:** `curl -s -D - -o /dev/null -H "Range: bytes=0-1023" https://<domain>/insight/dokumente/<id>/preview/`
  muss `206`, `Content-Range` und ein `ETag` zeigen, und `X-Accel-Redirect` darf nie beim Client ankommen.

Andere Webserver (z. B. nginx mit einer `internal`-Location): vorher prüfen, dass Typ, Anzeigeart, `nosniff`
und die Sandbox aus der Antwort der Anwendung beim Client ankommen.

### Ablage nach SHA-256

Gleiche Dateien (dieselbe Anlage an mehreren Vorgängen) liegen nur einmal in der Ablage (Issue #788,
`services/file_store.py`). Die Originale bleiben unverändert, es wird nichts komprimiert oder umgerechnet.

- **Referenzzählung:** Jede Datei (`OParlFile.blob`) ist eine Referenz auf ihren Inhalt (`OParlFileBlob`, Tabelle
  `oparl_file_blobs`). Ablegen, Ersetzen und Freigeben laufen in einer Transaktion mit gesperrter Zeile des Inhalts,
  die Datei wird verschoben, solange die Sperre gilt. Fällt die letzte Referenz weg (neue Fassung, Löschen nach
  Frist, `purge_deleted`, `prune_file_cache --unlisted`, Löschen einer Kommune), wird der Inhalt verwaist markiert;
  `dokumentablage --aufraeumen` löscht ihn nach zehn Minuten – lokal und im Objektspeicher. Eine falsche Zählung
  wird dabei berichtigt statt gelöscht.
- **Ein Abruf je Datei:** Der Ingestor lädt jede Datei für die Texterkennung gestreamt in eine temporäre Datei
  unter `sha256/tmp` (Größengrenze `TEXT_EXTRACTION_MAX_SIZE_MB` greift während des Downloads, nie liegt eine
  ganze Datei im Arbeitsspeicher), hasht dabei und legt sie danach selbst ab – nur gelistete Kommunen, nie
  unter `FILE_CACHE_MIN_FREE_GB` freiem Platz, keine Hinweisseiten statt der Datei. Dafür hängt der Dienst
  `ingestor` das Volume `mandari_files` unter `OPARL_FILES_ROOT` ein. Mit `INGESTOR_STORES_FILES=true` holt
  `cache_files` Dateien in der Texterkennung nicht ein zweites Mal (hängt die Erkennung länger als einen Tag,
  doch). Maßgeblich ist die letzte Änderung des Datensatzes: Auch ältere Dateien, die wieder auf „pending“ gehen
  (neue Fassung, Wiederfreigabe nach dem Löschabgleich), holt nur der Ingestor. Ausnahme vom Streaming: Ist
  `MISTRAL_API_KEY` gesetzt und reicht pypdf nicht, liest der Ingestor die Datei für die Mistral-OCR ganz ein
  (die Schnittstelle erwartet sie base64-kodiert in der Anfrage, bis `TEXT_EXTRACTION_MAX_SIZE_MB`); ohne
  Mistral rendert Tesseract seitenweise. Mit `TEXT_EXTRACTION_RUNNER=worker` ruht der Ingestor dabei,
  `INGESTOR_STORES_FILES` hat keine Wirkung mehr, und der Dokument-Cache holt alle Dateien selbst.
- **Rechte:** Abgelegte Inhalte sind für alle lesbar (`0644`), auch wenn der Download als temporäre Datei mit
  `0600` entstand. Anwendung und Ingestor legen mit derselben Kennung ab (Compose: uid 1000), der Webserver liest
  sie für die Auslieferung.
- **Umstellen des Bestands:** `python manage.py dokumentablage --umstellen --limit 5000 --trotz-zeitplan` verschiebt
  Kopien aus dem Layout je Kommune in die Ablage (kein zweiter Platzbedarf, wiederaufnehmbar, so oft wiederholen, bis
  `noch im alten Layout 0` erscheint). Doppelte Kopien entfallen dabei.
- **Pflege:** `dokumentablage` zeigt Inhalte, Belegung, die Ersparnis durch Deduplizierung und verwaiste Inhalte;
  `--referenzen` berechnet die Zähler aus den Verweisen neu; `--aufraeumen` läuft stündlich um :50 als Zeitplan
  `befehl:dokumentablage` im Worker (`DEPLOYMENT.md`, „Geplante Aufgaben“). Verwaiste Inhalte löscht außerdem
  jeder Lauf des Löschabgleichs (`loeschabgleich`), damit eine ersetzte oder gelöschte Fassung nicht an einem
  zweiten Zeitplan hängt. `cache_files --stats` nennt beide Größen: „belegt“ zählt jeden
  Inhalt einmal (plus Kopien im alten Layout), „je Datei gezählt“ zählt Dateien mit gleichem Inhalt mehrfach.

- **Von Hand:** Die Kennzahlen (`dokumentablage` ohne Schritt) laufen immer. Die Schritte brauchen
  `--trotz-zeitplan`, solange der Worker den Zeitplan bedient; ein alter Cron-Eintrag überspringt dann und gehört
  aus der Crontab entfernt. Abschalten: `EVENTS_SCHEDULES_DISABLED=befehl:dokumentablage`.

- **Rückfall auf ein älteres Image:** Ältere Images finden die Kopien über `local_path` weiter, legen neue aber im
  alten Layout ab. `purge_deleted`, `prune_file_cache` und `loeschabgleich` dürfen mit einem älteren Image nicht
  laufen, solange Inhalte geteilt sind: Sie löschen Dateien nach `local_path` und kennen keine Referenzen
  (`loeschabgleich` beim Ersetzen einer Fassung und beim Löschen nach Frist). Ein so altes Image kennt keine
  Zeitpläne dieser Befehle; aus einer gesicherten Crontab also keine Einträge dieser Befehle zurückspielen.

### Objektspeicher

Vorbereitet, **Standard aus**. Mit `OBJ_ENABLED=true` und den Zugangsdaten `OBJ_ENDPOINT`, `OBJ_BUCKET`, `OBJ_KEY`,
`OBJ_SECRET` (nur in der Umgebung, nie im Repo; `OBJ_REGION` optional, sonst aus dem Endpunkt abgeleitet) liegt die
Ablage in einem S3-kompatiblen Objektspeicher unter denselben Schlüsseln (`sha256/<ab>/<sha256>`):

- `dokumentablage --hochladen` lädt Inhalte ohne Kopie im Objektspeicher hoch (`remote_at`).
- Die lokale Ablage wird zum **Zwischenspeicher**: `dokumentablage --aufraeumen` verdrängt bei mehr als
  `OBJ_CACHE_MAX_GB` (Standard 60) die am längsten nicht gelesenen Inhalte – nur solche, die sicher im Objektspeicher
  liegen. Eine kleinere [Obergrenze der Gesamtgröße](#obergrenze-der-gesamtgröße) (`FILE_CACHE_MAX_TOTAL_GB`) wirkt
  zusätzlich. Jeder Abruf über die Vorschau vermerkt den letzten Zugriff in der Zugriffszeit (`atime`) der Datei,
  höchstens einmal je Stunde. Die Änderungszeit bleibt unberührt, denn aus ihr bildet der Webserver `ETag` und
  `Last-Modified`; so greifen bedingte Anfragen und Range-Anfragen mit `If-Range` weiter. Ein Mount mit
  `noatime` stört nicht, die Zeit wird ausdrücklich gesetzt.
- Ein neuer Inhalt geht im selben Abruf in den Objektspeicher (`cache_files`, Löschabgleich, Admin-Aktion; Issue
  #919), der Zeitplan holt nur Altbestand und gescheiterte Uploads nach. Die Vorschau legt ab, ohne in der Anfrage
  hochzuladen; verdrängt wird ohnehin nur, was im Objektspeicher liegt.
- Fehlt ein Inhalt lokal, holt die Vorschau ihn gestreamt aus dem Objektspeicher (ohne Datenbankverbindung
  festzuhalten, Hashprüfung, Gesamtdauer höchstens `OBJ_FETCH_TOTAL_SECONDS`, Standard 60, nur über
  `FILE_CACHE_MIN_FREE_GB`) und liefert ihn wie gewohnt aus. **Nicht vorhanden ist nicht gestört**
  (`file_store.local_copy`, `object_storage.exists`: vorhanden, fehlt, gestört): Antwortet der Objektspeicher
  nicht, ist er zu langsam oder die Ablage nicht eingehängt, ruft niemand bei der Quelle ab – die Vorschau
  antwortet mit 503 und `Retry-After`, der Abruf versucht es später. Eine Störung des Objektspeichers löst so nie
  eine Abrufwelle bei den Kommunen aus. Nur ein bestätigtes Fehlen (404 im Objektspeicher, falscher Hash) setzt
  die Datei auf `none`; dann holt sie die Vorschau bzw. `cache_files` neu, und der Inhalt wird neu hochgeladen.
- **Prüfsummen:** boto3 sendet seit 1.36 standardmäßig Prüfsummen im `aws-chunked`-Verfahren, was manche
  S3-kompatiblen Anbieter ablehnen. `OBJ_CHECKSUMS=when_required` (Standard) verhält sich wie frühere Versionen.
  Beim ersten Test mit dem echten Bucket Hochladen, Holen und Löschen prüfen; nur wenn der Anbieter es verlangt,
  `when_supported` setzen.
- **Zugangsdaten einbinden:** `docker-compose.yml` reicht `OBJ_ENDPOINT`, `OBJ_BUCKET`, `OBJ_KEY` und
  `OBJ_SECRET` per Variablenersetzung durch. Liegen sie in einer eigenen Umgebungsdatei statt in der `.env`,
  liest Compose diese zusätzlich: `docker compose --env-file .env --env-file <datei> up -d` (spätere Dateien
  gewinnen). Ein `env_file:` am Dienst reicht nicht, denn die Einträge unter `environment` (leer vorbelegt)
  gingen vor.
- Reihenfolge beim Einschalten: Bestand umstellen (`--umstellen`), Zugangsdaten setzen, `OBJ_ENABLED=true`,
  Neustart, `dokumentablage --hochladen --trotz-zeitplan` bis nichts mehr offen ist. Danach lädt der Zeitplan
  `befehl:dokumentablage` vor jedem Aufräumen selbst hoch (`--hochladen --aufraeumen`, solange `OBJ_ENABLED` gesetzt ist).
- Ausschalten: `OBJ_ENABLED=false`. Lokal verdrängte Inhalte gelten dann als fehlend; die Vorschau holt sie von der
  Quelle, `cache_files` lädt sie nach. Verwaiste Inhalte, die schon im Objektspeicher liegen, bleiben dort, solange er aus ist
  (`remote_kept` beim Aufräumen); das nächste Aufräumen mit eingeschaltetem Objektspeicher löscht sie. Eine
  [Obergrenze der Gesamtgröße](#obergrenze-der-gesamtgröße) setzt aus, solange Inhalte laut Datenbank im
  Objektspeicher liegen (siehe dort, „Moduswechsel“).

### Obergrenze der Gesamtgröße

Ohne weitere Einstellung wächst die Ablage, bis `FILE_CACHE_MIN_FREE_GB` greift. Mit `FILE_CACHE_MAX_TOTAL_GB`
(GB, Standard `0` = unbegrenzt) bleibt sie begrenzt (Issue #961, `services/file_cache_limit.py`):

- **Wann:** Das stündliche Aufräumen (`dokumentablage --aufraeumen`, Zeitplan `befehl:dokumentablage` um :50)
  verdrängt, sobald die Belegung die Grenze überschreitet, bis `FILE_CACHE_EVICT_TARGET_PERCENT` (Standard 90 %)
  der Grenze erreicht sind. `cache_files` lädt über den Abrufweg (`hub.ris.abruf`) nur bis zur Grenze nach und meldet
  sonst `limit`; das gilt ebenso für die [Ablage für alle Quellen](#ablage-für-alle-quellen-stichtag) mit
  `TEXT_EXTRACTION_RUNNER=worker`. Platz schafft der nächste Aufräumlauf, nie eine Anfrage. Was bis dahin sonst
  abgelegt wird (Vorschau, Löschabgleich, Ingestor), kommt hinzu: Die Grenze kann also für höchstens eine Stunde um
  die Ablagen dieser Stunde überschritten sein.
- **Belegung:** Inhalte unter `sha256/<ab>/` auf der Platte (ohne Teil-Downloads unter `sha256/tmp`) plus Kopien
  im alten Layout je Kommune (Größe aus der Datenbank).
- **Wer zuerst geht:** Dokumente, die am längsten nicht über die Vorschau ausgeliefert wurden; nie ausgelieferte
  nach dem Datum des Dokuments (Datum, sonst Anlage in der Quelle bzw. bei uns), ältere zuerst. Das ist dieselbe
  Einteilung wie im Zugriffsprotokoll, nach dem jüngere Dokumente weit häufiger gelesen werden. Ein Inhalt, den
  mehrere Dokumente teilen, zählt so jung wie das jüngste davon. Der Zeitpunkt der Zwischenspeicherung zählt
  bewusst nicht: Der Erstabgleich legte die neuesten Dokumente zuerst ab, sie wären sonst zuerst gegangen.
- **Mit Objektspeicher:** Gelöscht wird nur die lokale Kopie, und nur von Inhalten, die sicher im Objektspeicher
  liegen: Vor dem Löschen fragt jeder Lauf per `HEAD` nach, ob der Inhalt dort mit seiner Größe liegt. Fehlt er oder
  weicht die Größe ab, bleibt die Kopie, und `remote_at` wird zurückgesetzt (das nächste `--hochladen` überträgt ihn
  erneut); ist der Objektspeicher gestört, bricht der Lauf ab, ohne weiter zu verdrängen (nicht vorhanden ist nicht
  gestört, siehe [Zustände des Abrufs](#zustände-des-abrufs)). Die Datenbank bleibt unverändert, die Dokumente
  bleiben „Lokal vorhanden“; Vorschau und Texterkennung holen den Inhalt bei Bedarf aus dem Objektspeicher
  (`file_store.local_copy`), nie von der Quelle. Noch nicht hochgeladene Inhalte bleiben, bis `--hochladen` sie
  übertragen hat. `OBJ_CACHE_MAX_GB` wirkt daneben weiter (nach der Zugriffszeit der Datei); es gilt die kleinere
  Grenze.
- **Ohne Objektspeicher:** Alle Dokumente eines Inhalts geben ihre Referenz gemeinsam frei und gehen auf
  „Verdrängt“ (`local_status=evicted`); danach verweist kein Dokument mehr auf den Inhalt, und er wird gelöscht.
  `evicted` ist kein Zustand, aus dem der Abruf von selbst beansprucht: Weder `cache_files` noch das
  Sicherheitsnetz laden verdrängte Dokumente nach, sonst lüde der nächste Lauf wieder, was die Grenze eben
  verdrängt hat. Ausdrücklich geholt werden sie über den Abrufweg nur von der Vorschau bei Bedarf (Live-Abruf mit
  Write-Through, wie jeder Abruf dort mit robots.txt, Drossel je Host und Schonung) und von
  `cache_files --verdraengte` (etwa nach Anheben oder Abschalten der Grenze; ebenso die Admin-Aktion „Lokal
  zwischenspeichern“ von Hand). Scheitert der Live-Abruf, gelten dieselben Zustände wie bei `none`
  ([Zustände des Abrufs](#zustände-des-abrufs)): `404`/`410` in den ersten sieben Tagen nach unserer Erfassung wird
  wiederholt, später `missing`. Ein bestätigtes Fehlen der Kopie setzt ein verdrängtes Dokument nie auf `none`.
- **Text bleibt:** Extrahierter Text, Status der Texterkennung und Fingerabdruck (`sha256_hash`) bleiben unberührt;
  es wird nichts neu erkannt. Suche, Vorgangsseiten, OParl-Ausgabe und Löschabgleich arbeiten weiter wie bisher. Die
  Texterkennung beansprucht nur abgelegte Dokumente (`ok`): Ein verdrängtes Dokument ohne Text bleibt verdrängt,
  und `mandari_files_stored_without_text` zählt es nicht als abgelegt.
- **Nie verdrängt** werden Inhalte, deren Text gerade erkannt wird oder darauf wartet (Texterkennung
  `pending`/`processing`, eben beansprucht oder mit wartendem Auftrag `file.extract_text`, etwa einer Neuerkennung),
  Dokumente, die gerade abgerufen werden (`fetching`), Kopien aus der letzten Stunde und, ohne Objektspeicher,
  Dokumente, die sich nicht neu abrufen ließen: Quelle in Schonung oder nicht aktiv, Dateiabruf abgeschaltet,
  synthetische Quelle (Domäne `.invalid`, etwa die Demo), keine Download-Adresse oder eine Quelle, die Dateiabrufe
  verweigert. Letzteres gilt als erkannt, sobald ein Dokument der Quelle verweigert ist (`refused`: robots.txt oder
  HTML-Seite statt der Datei) oder einen älteren Vermerk der robots.txt trägt (Cache oder Texterkennung), die
  robots.txt im gemeinsamen Cache eine abgelegte Datei der Quelle sperrt (angefragt wird dafür nie) oder ein Abruf
  mit HTTP 401/403 endete. Das ist bewusst grob: Ein einziges solches Dokument schützt die ganze Quelle; eine
  Freigabe (`robots_override`, `dokumentkette freigeben`) bzw. ein gelungener Neuversuch
  (`cache_files --retry-errors`) hebt den Schutz auf. Reicht der Rest nicht bis zum Ziel, meldet der Lauf das mit
  den geschützten Größen je Grund.
- **Moduswechsel:** Liegen Inhalte laut Datenbank im Objektspeicher (`remote_at`), ist er im laufenden Container aber
  nicht konfiguriert (`OBJ_ENABLED=false` im Worker, fehlende `OBJ_*`), setzt die Grenze aus: Das stündliche Aufräumen
  meldet „Obergrenze ausgesetzt“ (Fehlerausgabe und Protokoll), `prune_file_cache` bricht ab. Ohne diesen Halt
  würden solche Inhalte freigegeben, verwaisten, und das nächste Aufräumen mit Objektspeicher löschte sie dort.
  `prune_file_cache --ohne-objektspeicher` macht ausdrücklich ohne Objektspeicher weiter; Inhalte mit `remote_at`
  bleiben auch dann unangetastet (geschützt als `im_objektspeicher`), auch wenn sie erst während des Laufs
  hochgeladen werden. Die erste Zeile von `prune_file_cache` nennt den Modus.
- **Nachprüfung unter der Sperre:** Zwischen Planung und Verdrängen prüft jeder Stapel in beiden Modi unter
  Zeilensperre erneut: Texterkennung (auch eben beansprucht oder eingereiht), laufender Abruf, frisch abgelegt,
  inzwischen ausgeliefert oder neu verwiesen (Rang jünger), Zahl der Verweise (eine gesperrte Datei des Inhalts hält
  ihn) und, ohne Objektspeicher, Quelle in Schonung bzw. Dateiabruf abgeschaltet (je Stapel neu gelesen),
  Download-Adresse und `remote_at`.
- **Sicherheit:** Gelöscht wird nur unterhalb von `OPARL_FILES_ROOT`, nie über symbolische Verweise (weder die
  Datei noch das Verzeichnis `sha256/<ab>` darf einer sein; Kopien im alten Layout müssen nach Auflösen aller
  Verweise darunter liegen). Liegt `sha256/` selbst außerhalb, wird dort nichts verdrängt.
- **Last:** Der Durchgang über die Platte summiert nur Größen. Die Rangfolge liefert die Datenbank über einen
  Cursor, verdrängt wird in Stapeln zu 200 Inhalten in kurzen Transaktionen; gerade gesperrte Zeilen (eine Datei
  wird eben abgelegt) werden übersprungen statt erwartet. Mit Objektspeicher kostet jeder verdrängte Inhalt eine
  `HEAD`-Anfrage (vor der Zeilensperre). Eine Sperre im gemeinsamen Cache verhindert, dass Zeitplan und Handlauf
  gleichzeitig verdrängen.
- **Kennzahlen:** Mit Objektspeicher weisen `cache_files --stats` und die Zustandsprüfung (Monitoring,
  „Dokument-Cache“) getrennt aus, was lokal auf der Platte liegt (Durchgang wie bei der Grenze) und was im
  Objektspeicher; „abgelegt“ zählt Dokumente mit Kopie an einem der beiden Orte. Ohne Objektspeicher bleibt es bei
  „lokal“ mit der Summe aus der Datenbank. `cache_files --stats` nennt außerdem die Zahl verdrängter Dokumente.

**Einmaliger Abbau eines großen Bestands:** erst ansehen, dann verdrängen, danach die Grenze setzen. Alle Befehle im
Container, in dem auch der Worker läuft (gleiche `OBJ_*`), sonst bricht der Befehl wegen des Moduswechsels ab:

```bash
# 1. Modus und Umfang ansehen (ändert nichts): erste Zeile „Modus: mit Objektspeicher“?
python manage.py prune_file_cache --max-gb 10 --dry-run
# 2. Mit Objektspeicher: Stichprobe per HEAD, ob die Inhalte, die lokal gingen, dort mit ihrer Größe liegen
python manage.py prune_file_cache --max-gb 10 --dry-run --pruefe-objektspeicher --stichprobe 500
#    fehlen welche: dokumentablage --hochladen --trotz-zeitplan, dann Schritt 2 wiederholen
#    (ohne --stichprobe prüft der Probelauf alle; das dauert je Inhalt eine Anfrage)
# 3. Verdrängen; mit Objektspeicher prüft der Lauf jede lokale Kopie vor dem Löschen per HEAD
python manage.py prune_file_cache --max-gb 10 --pruefe-objektspeicher
# 4. danach FILE_CACHE_MAX_TOTAL_GB=10 setzen (Anwendung und Worker) und neu starten
```

Eine lokale Kopie, deren Inhalt im Objektspeicher fehlt oder eine andere Größe hat, bleibt; in beiden Fällen wird
`remote_at` zurückgesetzt. So schützt „nicht hochgeladen“ die Kopie auch vor späteren Läufen, und das nächste
`dokumentablage --hochladen` überträgt den Inhalt erneut. Ist der Objektspeicher nicht erreichbar, bricht der Lauf
ab. Der echte Lauf prüft mit Objektspeicher immer (auch das stündliche Aufräumen); `--pruefe-objektspeicher` braucht
es nur im Probelauf, die Ausgabe nennt dann geprüft, vorhanden, fehlend und abweichend. Der Befehl läuft neben dem
Zeitplan; startet er, während das Aufräumen gerade verdrängt, endet er mit Hinweis. Ein abgebrochener Lauf
hinterlässt nichts Halbes und lässt sich wiederholen. `cache_files --stats` zeigt danach die Grenze, die Zahl
verdrängter Dokumente und die lokale Belegung.

### Löschabgleich

Entfernt oder ändert eine Kommune ein Dokument, verschwindet es auch bei uns (Issue #787,
`services/file_reconcile.py`):

- **Sperre sofort:** Ein Dokument ist gesperrt, wenn die Quelle es als gelöscht meldet (OParl `deleted`, der
  Ingestor markiert es) oder seine Download-Adresse `404`/`410` liefert (`source_missing_since`). Gesperrt
  heißt: Die Vorschau antwortet mit `410` und liefert keine Bytes, auch nicht aus der lokalen Kopie; die
  Vorgangsseite zeigt weder Dokument noch Text; der OParl-Objekt-Endpunkt (`/oparl/v1/file/<id>`) gibt kein
  Feld `text` mehr aus; Suchindex (auch nach einem vollständigen Neuaufbau) und KI-Zusammenfassung des
  Vorgangs verlieren es, eine neue Zusammenfassung nimmt seinen Text nicht auf; der Ingestor erkennt keinen
  Text mehr und nimmt die Datei nicht wieder in den Index. Liefert die Quelle das Dokument wieder, wird die
  Sperre aufgehoben. Browser und Zwischenspeicher dürfen ein vorher ausgeliefertes Dokument noch bis zu 24 h
  zeigen (`Cache-Control: public, max-age=86400`); das liegt bewusst innerhalb der Vorgabe des Konzepts.
- **Änderung per Hash:** Meldet die Quelle eine Änderung (`modified`) nach unserer Kopie bzw. Texterkennung,
  lädt der Abgleich die Datei neu und vergleicht den SHA-256. Anderer Inhalt ersetzt die Kopie, der alte Text
  wird verworfen und vom Ingestor neu erkannt; gleicher Inhalt ändert nichts. Hat die Datei noch keine Kopie und
  wird sie abgelegt, legt der Abgleich den geladenen Inhalt gleich ab (kein zweiter Abruf, Issue #919). Der
  Abgleich ruft über `hub.ris.abruf` ab und überspringt Dateien, die gerade abgerufen werden.
- **Stichproben:** Gedrosselte HEAD-Anfragen auf die Download-Adressen (am längsten nicht geprüfte zuerst)
  finden Löschungen, die die Quelle nicht meldet. Ein `404`/`410` (oder ein Server ohne HEAD) wird per GET
  bestätigt, eine abweichende Größe per Hash abgeglichen. Liefert HEAD eine HTML-Seite statt der Datei
  (weiche 404), entscheidet ebenfalls ein GET; eine Hinweisseite gilt nie als „vorhanden“. Mindestabstand je
  Host (`--interval`), höchstens `--head-limit` Anfragen je Kommune und Lauf. Antwortet ein Host mit `429`
  oder `503`, fragt der Lauf ihn nicht weiter an (auch kein GET hinterher); nach fünf Fehlern in Folge ebenso.
  Stichproben laufen nur für gelistete Kommunen (ausgeblendete zeigen nichts öffentlich); mit `--body` für
  genau diese Kommune. Quellen in Schonung oder mit abgeschaltetem Dateiabruf bleiben unberührt. Geprüft werden
  auch Dateien, deren Abruf mit `404`/`410` endete (`local_status = missing`), damit auch nie abgelegte Dateien
  gesperrt werden; liefert die Quelle eine solche Datei wieder, wird sie abgelegt bzw. neu abgerufen.
- **Erneut prüfen:** Ein wegen `404`/`410` gesperrtes Dokument prüft der Abgleich nach 1, 7 und 25 Tagen
  erneut (vor den übrigen Stichproben) und unmittelbar vor dem Löschen noch einmal per GET. Liefert die
  Quelle es wieder, wird entsperrt statt gelöscht. Eine vorübergehende `404` (Wartung, Umstellung, eine
  Firewall) versteckt ein Dokument also höchstens bis zur nächsten Prüfung und löscht nichts.
- **Bremse:** Liefern in einem Lauf mehr als `FILE_RECONCILE_MAX_MISSING` (Standard 10, `--max-fehlend`)
  Dokumente einer Quelle neu `404`/`410`, sperrt der Lauf keines davon und lässt die Quelle für den Rest des
  Laufs in Ruhe. Die Ausgabe nennt sie (`gebremst=…`, Hinweis auf stderr). Dann die Quelle prüfen (neue
  Adressen nach einer Umstellung, Wartung, Sperre unserer Abrufe); bei einer echten Massenlöschung den Lauf mit
  höherem `--max-fehlend` für diese Kommune wiederholen.
- **robots.txt ist verbindlich:** Vor jedem Abruf einer Datei prüft der Abgleich die robots.txt des Hosts
  (mit `*` und `$` nach RFC 9309, je Host einen Tag zwischengespeichert; nicht lesbar = kein Abruf). Eine
  Ausnahme trägt nur eine Quelle mit Vermerk in `sync_config["robots_override"]`, z. B.
  `{"scope": "files", "note": "Zustimmung liegt vor, Anfrage läuft"}` (Bereich `files` oder `all`, Vermerk
  mindestens zehn Zeichen; dasselbe Format wie für die übrigen Abrufe der Quelle).
  `loeschabgleich --robots` listet je Quelle, ob die robots.txt Dateiabrufe sperrt, samt Ausnahmen.
- **Löschen nach Frist:** Nach `FILE_PURGE_AFTER_DAYS` (Standard 30) Tagen Sperre löscht der Abgleich die lokale
  Kopie und den extrahierten Text (`content_purged_at`). Der Datensatz bleibt als Tombstone. Hebt die Quelle die
  Löschung später auf, wird der Text neu erkannt und die Kopie nachgeladen. Nicht mehr abrufbare Dokumente
  (`404`/`410`) fragt der Abgleich vorher noch einmal per GET ab (gedrosselt, robots.txt); lässt sich die Quelle
  nicht befragen (Fehler, robots.txt, Schonung), wartet er bis zu `FILE_PURGE_CONFIRM_GRACE_DAYS` (Standard 7)
  Tage und löscht danach ohne Rückfrage. In der Quelle gelöschte Dokumente ohne Löschzeitpunkt (Altbestand) bekommen beim ersten Lauf den
  aktuellen Zeitpunkt; ihre Frist beginnt also dann und nicht rückwirkend.

Der Abgleich läuft stündlich um :15 als Zeitplan `befehl:loeschabgleich` im Worker (Zeitgrenze 50 Minuten;
`DEPLOYMENT.md`, „Geplante Aufgaben“). **Vor dem ersten Lauf `loeschabgleich --robots` ansehen** (läuft immer,
auch neben dem Zeitplan): Die Liste zeigt, welche Quellen der Abgleich wegen ihrer robots.txt nicht abfragt. Wer
das vor dem ersten Lauf prüfen will, setzt vor dem Update `EVENTS_SCHEDULES_DISABLED=befehl:loeschabgleich` bei
Worker und Anwendung und entfernt den Schalter danach; derselbe Schalter schaltet den Abgleich ab. Von Hand
läuft er mit `--trotz-zeitplan`.

Ein Lauf gleicht höchstens 200 geänderte Dokumente ab (`--changed-limit`) und nimmt je Kommune 30 Stichproben
(`--head-limit`). `--nur-loeschen` und `--ohne-loeschen` trennen die Schritte. Ein Lauf hält eine Sperre im
gemeinsamen Cache: Startet der nächste, bevor der vorige fertig ist, endet er sofort mit einem Hinweis. Die
Sperre verfällt nach 3000 s; für einen längeren Handlauf den Zeitplan so lange abschalten.

### Quellen-Schonung

Ratsinformationssysteme sperren IP-Adressen, die zu viele Verbindungen aufbauen (Köln, Issue #89).
Sobald der Ingestor eine Quelle `INSIGHT_SOURCE_BACKOFF_FAILURES`-mal in Folge nicht erreicht hat,
lassen `cache_files` und der Vorschau-Proxy diese Quelle in Ruhe (Proxy antwortet mit 503 und
`Retry-After`). Der Ingestor selbst verdoppelt den Abstand zwischen den Versuchen (10, 20, 40 …
Minuten, höchstens 6 Stunden). Ein erfolgreicher Sync setzt den Zähler zurück, danach läuft alles
automatisch weiter. Der Betriebsmonitor zeigt die Schonung als Grund an der Quelle.

## Storage Box je Kommune mounten (Beispiel CIFS)

```bash
apt install cifs-utils
mkdir -p /srv/mandari-files/stadt-koeln
cat > /root/.smb-koeln <<EOF
username=<box-user>
password=<box-passwort>
EOF
chmod 600 /root/.smb-koeln
echo "//<box-host>/backup /srv/mandari-files/stadt-koeln cifs credentials=/root/.smb-koeln,uid=1000,gid=1000,iocharset=utf8,_netdev,nofail 0 0" >> /etc/fstab
mount -a
```

Anwendung und Worker sehen `/srv/mandari-files` als `/app/files` (beide brauchen denselben Mount,
weil der Zeitplan im Worker den Cache füllt); die Kommune landet automatisch im gemounteten
Unterverzeichnis. Nach dem Mount einmal
`docker compose exec mandari python manage.py cache_files --body <slug> --limit 100000 --trotz-zeitplan`
für den Erstbestand ausführen (ca. 65 GB, dauert mehrere Stunden). Für diese Zeit
`EVENTS_SCHEDULES_DISABLED=befehl:cache_files` bei Worker und Anwendung setzen: Die Sperre des
Befehls verfällt nach 3000 s, sonst startet der stündliche Zeitplan parallel und fragt dieselben
Quellen doppelt an. Danach den Schalter wieder entfernen.
