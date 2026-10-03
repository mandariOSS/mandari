# Lokaler Dokument-Cache (Insight)

Alle OParl-Dateien (Vorlagen, Anlagen, Niederschriften – praktisch nur PDFs) werden lokal
zwischengespeichert. Vorschau und Download kommen dann von der Platte, unabhängig davon, ob das
Ratsinformationssystem gerade erreichbar ist (Issue #87). Der Proxy hält keine Worker mehr
minutenlang fest (Issue #86): kurze Timeouts, lokale Datei zuerst.

## Speicherlayout

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
| `FILE_PROXY_TIMEOUT_SECONDS` | 15 | Lese-Timeout des Proxys für Live-Abrufe |
| `RIS_REQUEST_INTERVAL` | 1.0 | Drossel je Host: Mindestabstand in Sekunden zwischen zwei Anfragen an dasselbe RIS, gemeinsam mit dem Ingestor über Redis (je Quelle: `sync_config.request_interval`, 0 = aus) |
| `FILE_PROXY_PACE_MAX_WAIT_SECONDS` | 5 | So lange warten Vorschau und KI-Zusammenfassung höchstens auf ihren Zeitpunkt (samt Abruf einer noch nicht zwischengespeicherten robots.txt), sonst HTTP 503 mit `Retry-After` bzw. die Bitte um einen neuen Versuch. Gewartet wird mit belegtem Abrufplatz (`FILE_PROXY_MAX_CONCURRENT`) und ohne gehaltene Datenbankverbindung |
| `INSIGHT_SOURCE_BACKOFF_FAILURES` | 3 | Ab so vielen Sync-Fehlversuchen in Folge werden Cache-Nachladen und Live-Abruf für die Quelle pausiert |
| `FILE_PURGE_AFTER_DAYS` | 30 | Kopie und Text gesperrter Dokumente nach so vielen Tagen löschen (Löschabgleich) |
| `FILE_PURGE_CONFIRM_GRACE_DAYS` | 7 | Lässt sich die Quelle vor dem Löschen nicht befragen, wartet das Löschen höchstens so viele Tage zusätzlich |
| `FILE_RECONCILE_MAX_MISSING` | 10 | Bremse des Löschabgleichs: Liefern in einem Lauf mehr Dokumente einer Quelle neu `404`/`410`, wird keines gesperrt |
| `FILE_ACCEL_REDIRECT` | `false` | Lokale Kopien liefert der Webserver aus statt Django (siehe [Auslieferung über den Webserver](#auslieferung-über-den-webserver)) |

```cron
40 * * * * docker exec mandari python manage.py cache_files --limit 400 >> /var/log/mandari-file-cache.log 2>&1
```

- Der Cron lädt neueste Dokumente zuerst nach; jeder Live-Abruf über die Vorschau legt die Datei
  ebenfalls ab (Write-Through).
- `cache_files --stats` zeigt Abdeckung, Belegung und freien Speicher; der Betriebsmonitor hat
  dafür den Check „Dokument-Cache“.
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
  Mit der Auslieferung über den Webserver laden PDF-Betrachter große Dokumente in Teilen (Range-Anfragen),
  und jede Anfrage läuft durch Django. Gezählt wird nur die erste Anfrage eines Abrufs (ohne `Range` oder mit
  einem Bereich ab Byte 0); Folgeanfragen zählen weder als Abruf noch mit ihrer Größe.

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
		include Content-Type Content-Disposition X-Content-Type-Options Content-Security-Policy Cache-Control X-Mandari-Cache X-Request-ID
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
  wird verworfen und vom Ingestor neu erkannt; gleicher Inhalt ändert nichts.
- **Stichproben:** Gedrosselte HEAD-Anfragen auf die Download-Adressen (am längsten nicht geprüfte zuerst)
  finden Löschungen, die die Quelle nicht meldet. Ein `404`/`410` (oder ein Server ohne HEAD) wird per GET
  bestätigt, eine abweichende Größe per Hash abgeglichen. Liefert HEAD eine HTML-Seite statt der Datei
  (weiche 404), entscheidet ebenfalls ein GET; eine Hinweisseite gilt nie als „vorhanden“. Mindestabstand je
  Host (`--interval`), höchstens `--head-limit` Anfragen je Kommune und Lauf. Antwortet ein Host mit `429`
  oder `503`, fragt der Lauf ihn nicht weiter an (auch kein GET hinterher); nach fünf Fehlern in Folge ebenso.
  Stichproben laufen nur für gelistete Kommunen (ausgeblendete zeigen nichts öffentlich); mit `--body` für
  genau diese Kommune. Quellen in Schonung oder mit abgeschaltetem Dateiabruf bleiben unberührt.
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

```cron
15 * * * * docker exec mandari python manage.py loeschabgleich >> /var/log/mandari-loeschabgleich.log 2>&1
```

Ein Lauf gleicht höchstens 200 geänderte Dokumente ab (`--changed-limit`) und nimmt je Kommune 30 Stichproben
(`--head-limit`). `--nur-loeschen` und `--ohne-loeschen` trennen die Schritte. Ein Lauf hält eine Sperre im
gemeinsamen Cache: Startet der nächste, bevor der vorige fertig ist, endet er sofort mit einem Hinweis.

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

Der Container sieht `/srv/mandari-files` als `/app/files`; die Kommune landet automatisch im
gemounteten Unterverzeichnis. Nach dem Mount einmal `cache_files --body koeln --limit 100000`
für den Erstbestand ausführen (ca. 65 GB, dauert mehrere Stunden).
