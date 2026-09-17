# Betriebsmonitor (Admin)

Der Betriebsmonitor macht Ausfälle sichtbar, die vorher still blieben — etwa die Bonner
OParl-Quelle, die ab Februar 2026 nur noch HTTP 403 lieferte, ohne dass es jemand bemerkte.

## Wo

- **Admin-Dashboard** (`/admin/`): Panel „Betriebsstatus“ ganz oben mit Gesamtstatus, kritischen
  Quellen und Handlungsbedarf.
- **Betriebsmonitor** (`/admin/monitoring/`, Sidebar „Sync & Quellen“): alle Quellen mit Bewertung,
  Systemchecks (Datenbank, Cache, Elasticsearch, Ingestor-Daemon, Sync-Läufe 24 h), Handlungsbedarf,
  letzte Sync-Läufe.
- **OParl-Quellen-Liste**: Spalte „Gesundheit“, Filter nach Status, Felder *Letzter Fehler*,
  *Fehlversuche in Folge*, *Alarm gesendet am*.
- **Insight-Portal**: Ist die Quelle einer Kommune länger als die kritische Schwelle nicht
  synchronisiert, sehen Besucher:innen einen Hinweis „Datenstand: TT.MM.JJJJ“.

## Bewertung einer Quelle

| Status | Bedingung |
|--------|-----------|
| OK | letzter erfolgreicher Sync jünger als `INSIGHT_SOURCE_STALE_WARNING_HOURS` (Standard 48 h) |
| Warnung | älter als Warnschwelle **oder** letzter Versuch fehlgeschlagen |
| Kritisch | älter als `INSIGHT_SOURCE_STALE_CRITICAL_DAYS` (Standard 7 Tage) **oder** ≥ 3 Fehlversuche in Folge **oder** seit > 7 Tagen angelegt und nie synchronisiert **oder** Fehlerklasse „User-Agent gesperrt“ / „5xx-Serie“ |
| Inaktiv | `is_active = false` — erscheint nur im Admin-Filter der Quellenliste, nicht im Betriebsmonitor |

Der **Ingestor** schreibt bei jedem fehlgeschlagenen Versuch `last_error`, `last_error_at` und
zählt `consecutive_failures` hoch (`storage.record_source_failure`); ein erfolgreicher Sync setzt
die Werte zurück. So ist der konkrete Grund (z. B. `HTTP 403`) direkt im Admin sichtbar.

Ab `INSIGHT_SOURCE_BACKOFF_FAILURES` (Standard 3) Fehlversuchen greift die **Quellen-Schonung**:
Dokument-Cache und Datei-Proxy pausieren für diese Quelle, der Ingestor verdoppelt den Abstand
zwischen den Versuchen bis auf 6 Stunden (siehe `docs/FILE_CACHE.md`).

### Sperren und 5xx-Serien (Fehlerklassen)

Drei Störungsbilder erkennt der Ingestor selbst und ordnet sie einer **Fehlerklasse**
(`last_error_kind`) zu; der Betriebsmonitor zeigt dazu Grund und Handlungsempfehlung, die
Alarmmail nennt beides (Issue #123):

| Fehlerklasse | Erkennung | Was der Ingestor tut |
|---|---|---|
| `ua_blocked` — „User-Agent gesperrt“ | Ein Endpunkt antwortet mit HTTP 403. Der Client stellt daraufhin **genau eine** Vergleichsanfrage mit neutralem Client-Header (`python-httpx/<Version>`). Kommt darauf eine normale Antwort, filtert die Quelle gezielt auf unseren User-Agent. | Befund mit Zeitstempel in Sync-Log und Quellenstatus; die Quelle wird ab dem ersten Befund geschont (frühestens nach 60 Minuten wieder, danach wachsend bis 6 Stunden). Der Regelbetrieb läuft weiter mit unserem User-Agent — **keine Umgehung**. |
| `robots_blocked` — „robots.txt sperrt“ | Scraper-Quellen (#116): Die robots.txt der Instanz verbietet unserem User-Agent den Abruf der Basis-URL oder einer Seite. | Kein Crawl, keine Umgehung. Fehlerklasse mit Grund und Empfehlung an der Quelle; Schonung mit täglicher Nachprüfung (robots.txt ändert sich selten). Handlungsempfehlung: Betreiber um Freigabe unseres User-Agents in der robots.txt oder um die OParl-Schnittstelle bitten (Textvorschlag unten, sinngemäß). |
| `server_error_series` — „5xx-Serie“ | Ab `OPARL_SERVER_ERROR_SERIES_THRESHOLD` (Standard 5) aufeinanderfolgenden 5xx-Antworten je Host. | Sync-Warnung mit Statistik (Anzahl, Zeitraum, letzte Statuscodes, **betroffene Objektlisten**) statt stiller Lücke; Schonung ab dem ersten Befund (frühestens nach 30 Minuten). Eine erfolgreiche Antwort beendet die Serie. |

Die Statistik steht im Feld *Letzter Fehler* der Quelle und in den Details des Sync-Protokolls
(`error_kind`, `host_findings`). Nicht abrufbare Objektlisten (etwa die Sitzungsliste einer
Kommune) tauchen seit #123 als Fehler des Laufs auf, auch wenn der Rest der Quelle durchkam.

Ist ein Wert mit dem Betreiber vereinbart, lässt sich der User-Agent **je Quelle** im Admin
setzen (Feld *User-Agent*, leer = Standard). Den Standard beschreibt
`docs/SCRAPER_SOURCES.md`, Abschnitt Politeness. Die Vergleichsanfrage lässt sich mit
`OPARL_UA_PROBE_ENABLED=false` abschalten.

Handlungsempfehlung bei „User-Agent gesperrt“: den Betreiber ansprechen. Neutraler Textvorschlag
(Angaben in spitzen Klammern ersetzen):

> Sehr geehrte Damen und Herren,
>
> wir betreiben mit mandari (https://mandari.de) eine offene Plattform, die Ratsinformationen
> über die OParl-Schnittstelle Ihres Ratsinformationssystems bereitstellt. Seit dem <Datum>
> beantwortet Ihr System unsere Abrufe unter <URL des OParl-Endpunkts> mit HTTP 403, während
> derselbe Abruf mit anderen Clients funktioniert. Wir vermuten daher eine Regel, die auf
> unseren User-Agent „<User-Agent>“ reagiert.
>
> Unsere Abrufe sind bewusst sparsam (kleine Parallelität, Pausen zwischen Anfragen,
> Abruf nur geänderter Objekte); den Abstand passen wir gern an Ihre Vorgaben an. Könnten Sie
> unseren User-Agent freischalten oder uns mitteilen, unter welcher Kennung wir die
> Schnittstelle abrufen dürfen? Für Rückfragen erreichen Sie uns unter support@mandari.de.
>
> Mit freundlichen Grüßen
> <Name>, mandari

## Liveness und Readiness

Zwei Endpunkte, zwei Fragen (Issue #231):

| Endpunkt | Frage | Prüft | Bei Fehler |
|---|---|---|---|
| `/health/live/` | Antwortet der Prozess? | nichts weiter | Prozess neu starten (Liveness-Probe) |
| `/health/ready/` | Kann die Instanz Anfragen bedienen? | Datenbank, Cache (Redis), Elasticsearch, Medienspeicher, je 2 s Zeitlimit | keine Anfragen zuteilen (Readiness-Probe), **kein** Neustart |
| `/health/` | bisheriger Check (Datenbank) | Datenbank | bleibt für Compose-Healthcheck und Statusseite |

`/health/ready/` liefert 503 und `"status": "error"`, sobald eine Prüfung scheitert oder ins
Zeitlimit läuft; die Antwort nennt je Prüfung Ergebnis, Detail und Dauer. Ein Ausfall von
Redis oder Elasticsearch macht die Readiness rot, die Liveness bleibt davon unberührt.
Prüfungen, die für eine Installation nicht kritisch sind, lassen sich mit
`HEALTH_READY_OPTIONAL=elasticsearch` (kommagetrennt) als optional erklären: Sie werden
weiter gemeldet (`"status": "degraded"`), die Antwort bleibt 200.

Das Helm-Chart nutzt `live` für Startup- und Liveness-Probe und `ready` für die
Readiness-Probe. Die Statusseite (Gatus) kann `/health/ready/` als Bedingung nehmen.

## Alarmierung

```cron
15 * * * * docker exec mandari python manage.py check_source_health >> /var/log/mandari-source-health.log 2>&1
```

- Kritische Quelle → E-Mail-Alarm, Wiederholung frühestens nach 7 Tagen (`health_alert_sent_at`)
- Quelle wieder OK → Entwarnung, Zeitstempel wird gelöscht
- Ingestor-Daemon > 6 h ohne Lauf → Alarm, max. einmal je 24 h
- Empfänger: `INSIGHT_ALERT_EMAILS` (kommagetrennt), sonst `INSIGHT_MODERATION_EMAILS`, sonst Superuser

Bericht ohne Versand: `python manage.py check_source_health --report`

## Metriken

`/metrics/` liefert Anwendungsmetriken im Prometheus-Textformat (Issue #231; Code in
`apps/common/metrics.py`). Alle Werte gelten je Prozess und beginnen beim Neustart bei null –
das ist für Prometheus normal (`rate()`/`increase()` rechnen Neustarts heraus).

| Metrik | Labels | Bedeutung |
|---|---|---|
| `mandari_http_request_duration_seconds` (Histogramm) | `view`, `status_class` | Antwortzeit je View |
| `mandari_http_requests_total` | `view`, `status_class` | Anfragen je View und Statusklasse (`2xx` … `5xx`) |
| `mandari_http_request_errors_total` | `view` | Antworten mit Status 5xx |
| `mandari_db_pool_connections` | `state` (`in_use`, `available`, `min`, `max`) | Belegung des psycopg-Pools |
| `mandari_db_pool_requests_waiting` | – | Anfragen, die auf eine Pool-Verbindung warten |
| `mandari_db_connections_open` | – | nur ohne Pool: offene Verbindungen laut `pg_stat_activity` |
| `mandari_cache_keyspace_hits_total`, `…_misses_total`, `mandari_cache_hit_ratio` | – | Redis `INFO stats` (serverweit); ohne Redis-Backend nicht vorhanden |
| `mandari_emails_total` | `result` (`sent`, `failed`) | Versandversuche über `apps.common.email` |
| `mandari_pdf_documents_total`, `mandari_pdf_generation_seconds` | `result` | PDF-Erzeugung an der zentralen Stelle `apps.common.pdf.html_to_pdf` |
| `mandari_transcription_jobs` | `status` | wartende und laufende Transkriptionsaufträge |

`view` ist der URL-Name samt Namensraum (z. B. `session:meeting_detail`), nie der konkrete
Pfad – sonst würde jede ID ein neues Label erzeugen. Nicht auflösbare Pfade laufen unter
`unresolved`; der Abruf von `/metrics/` selbst wird nicht gezählt.

**Zugriff:** Der Endpunkt antwortet nur Absendern aus `METRICS_ALLOWED_NETWORKS`
(kommagetrennte CIDRs; Vorgabe Loopback und private Netze, also auch das Compose-Netz) oder
mit `Authorization: Bearer <METRICS_TOKEN>`. Alle anderen bekommen **404**, nicht 403 – der
Endpunkt soll von außen nicht einmal bestätigt werden. Die Absenderadresse kommt aus
`X-Forwarded-For`, das Caddy vor der Anwendung durch die echte Adresse ersetzt; ein anderer
Reverse-Proxy muss das genauso tun, sonst darf `METRICS_ALLOWED_NETWORKS` nur das Proxy-Netz
enthalten und Prometheus nutzt das Token.

Beispiel-Scrape-Konfiguration: `deploy/monitoring/prometheus-scrape.example.yml`;
Grafana-Vorlage (p95-Latenz je View, Fehlerquote, Pool-Belegung, Cache-Trefferquote):
`deploy/monitoring/grafana-mandari.json`. Der Ingestor liefert seine eigenen Metriken
(`mandari_ingestor_*`) weiterhin über seinen Port.

## Service-Level-Alarme

```cron
30 6 * * * docker exec mandari python manage.py check_service_levels >> /var/log/mandari-service-levels.log 2>&1
```

`check_service_levels` prüft täglich und meldet Unterschreitungen per E-Mail an
`INSIGHT_ALERT_EMAILS` (Code in `apps/common/service_levels.py`):

| Prüfung | Schwelle (Einstellung) | Quelle |
|---|---|---|
| Speicherplatz Medienverzeichnis und Wurzeldateisystem | frei ≥ 10 % **und** ≥ 2 GB (`SERVICE_LEVEL_DISK_MIN_FREE_PERCENT`, `…_GB`) | `shutil.disk_usage` |
| TLS-Zertifikate der eigenen Domains | Restlaufzeit ≥ 14 Tage (`SERVICE_LEVEL_TLS_MIN_DAYS`); auch „nicht prüfbar“ ist ein Alarm | TLS-Handschlag mit dem Host aus `SITE_URL` und `MONITOR_TLS_HOSTS` |
| Fehlerquote 5xx | ≤ 1 % bei mindestens 100 Anfragen (`SERVICE_LEVEL_ERROR_RATE_MAX_PERCENT`, `…_MIN_REQUESTS`) | `/metrics/` der laufenden Instanz (`METRICS_URL`, Vorgabe `http://127.0.0.1:8000/metrics/`) |
| Warteschlange Transkription | ältester wartender Auftrag ≤ 120 min (`SERVICE_LEVEL_QUEUE_MAX_AGE_MINUTES`) | `minutes.TranscriptionJob` |

Zur Fehlerquote ehrlich: Die Zähler leben im Web-Prozess und beginnen bei jedem Neustart bei
null. Der Lauf merkt sich deshalb den letzten Zählerstand im Cache und bewertet die Differenz
seit dem letzten Lauf – bei täglichem Cron die letzten ~24 h. Liegt der Zähler unter dem
gemerkten Stand (Neustart), gilt der Stand seit dem Start. Sind die Metriken nicht abrufbar,
ist das selbst ein Alarm.

Django-Tasks laufen mit dem `ImmediateBackend` synchron; eine allgemeine Warteschlange, die
sich stauen könnte, gibt es nicht. Geprüft wird darum nur die Transkriptions-Warteschlange.

Jeder Alarm geht **höchstens einmal je 24 h** hinaus (Sperre im Cache je Prüfobjekt);
alle fälligen Alarme eines Laufs stehen in einer Sammelmail. Entwarnungen werden nicht
verschickt. `--report` zeigt alle Befunde ohne Versand, `--dry-run` zeigt fällige Alarme,
ohne die Sperre zu setzen.

## Verfügbarkeitsbericht

```cron
15 0 1 * * docker exec mandari python manage.py availability_report --out /var/lib/mandari/reports/verfuegbarkeit-$(date -d "yesterday" +\%Y-\%m).md >> /var/log/mandari-availability.log 2>&1
```

`availability_report --month YYYY-MM [--gatus-url URL] [--out datei.md] [--target 99.5]`
stellt aus der Statusseite (Gatus, `GATUS_URL`) die Verfügbarkeit je überwachtem Dienst –
Bürgerportal, Work, Session, OParl-API, je nachdem, was die Statusseite überwacht – als
Markdown zusammen: Verfügbarkeit, Zielwert (99,5 % laut Konzept), Anzahl und Dauer der
Störungen, Gesamtverfügbarkeit als Mittel über die Dienste. Ohne `--month` gilt der Vormonat;
ohne erreichbare Statusseite endet der Lauf mit Exit-Code 1.

Grenzen, die im Bericht selbst stehen:

- Gatus kennt Verfügbarkeit nur für 1 h, 24 h, 7 d und 30 d, keinen Kalendermonat. Der
  Bericht nimmt den 30-Tage-Wert als Näherung; am Monatsersten für den Vormonat erzeugt,
  weicht er höchstens um einen Tag ab. Störungen und Ausfallzeit werden dagegen exakt aus den
  Ereignissen des Monats berechnet (`UNHEALTHY` → `HEALTHY`), daraus auch eine zweite
  Verfügbarkeitszahl „aus Ereignissen“.
- **Je Mandant** lässt sich nichts trennen: Alle Dienste laufen auf derselben Instanz, ein
  Ausfall trifft alle Mandanten gleich. Der Bericht gilt je Dienst und für alle Mandanten
  gemeinsam; für einen SLA-Nachweis gegenüber einem einzelnen Mandanten ist er damit
  vollständig, nur nicht mandantenspezifisch beschriftet.
