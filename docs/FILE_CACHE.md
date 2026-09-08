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

```cron
40 * * * * docker exec mandari python manage.py cache_files --limit 400 >> /var/log/mandari-file-cache.log 2>&1
```

- Der Cron lädt neueste Dokumente zuerst nach; jeder Live-Abruf über die Vorschau legt die Datei
  ebenfalls ab (Write-Through).
- `cache_files --stats` zeigt Abdeckung, Belegung und freien Speicher; der Betriebsmonitor hat
  dafür den Check „Dokument-Cache“.
- `purge_deleted` entfernt lokale Kopien getilgter Dateien.

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
