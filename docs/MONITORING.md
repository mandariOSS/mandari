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

## CSP-Verstoßmeldungen

Die Report-Only-Policy (#172) meldet Verstöße an `/csp-report/`. Der Endpunkt gehört zur
Anwendung, nicht zur Website: In der Caddy-Konfiguration muss der Pfad in der App-Pfadliste
stehen (`Caddyfile`, Matcher `@mandari_app`), sonst landen die Meldungen auf der Marketing-
Website (403). Auswertung: `journalctl CONTAINER_NAME=<app> | grep CSP-Verstoß` und die
Metrik `mandari_csp_violations_total{directive}`; Limit 60 Meldungen je Adresse und Minute.

### Scraper-Quellen: Parse-Quote und Zufluss (Pilot #53)

Für Quellen mit `source_type: scraper:*` bewertet der Betriebsmonitor zusätzlich den letzten
Lauf (`sync_config.scraper_state.last_run`, vom Ingestor nach jedem Lauf abgelegt):

| Befund | Schwelle | Status |
|---|---|---|
| Parse-Quote unter der Pilot-Schwelle | `INSIGHT_SCRAPER_QUOTA_WARN` (Standard 0,95), ab 5 Detailseiten | Warnung |
| Parse-Quote eingebrochen | `INSIGHT_SCRAPER_QUOTA_CRITICAL` (Standard 0,80) | Kritisch – typisch für ein Frontend-Redesign der Instanz (Parser-Bruch) |
| Voll-Lauf ohne gespeicherte oder erkannte Entitäten | – | Warnung (robots, Sperren, Kalenderfenster prüfen) |

Die Befunde erscheinen mit Empfehlung im Betriebsmonitor und in der Alarmmail von
`check_source_health`; die Ingestor-Metriken `mandari_ingestor_scraper_parse_quota` und
`mandari_ingestor_scraper_parse_failures_total` liefern den Verlauf für Grafana.

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

Die Prüfung `worker` ist immer optional: Braucht die Installation einen Worker
(`TASKS_BACKEND=journal`, `INGESTOR_EVENTS_ENABLED=true` oder `EVENTS_WORKER_REQUIRED=true`) und
bedient keiner die nötigen Rollen – mit `tasks` jede Warteschlange –, melden `/health/ready/` und
`/health/` (Feld `worker`) `"degraded"`; Admin-Startseite und Betriebsmonitor zeigen einen
Hinweis. Ohne Bedarf steht dort „nicht erforderlich“, nichts wird gemeldet, und die Prüfung fragt
die Datenbank nicht (DEPLOYMENT.md, „Worker“).

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
| `mandari_events_sequencer_blocked_seconds` | – | Ereignistechnik: wie lange eine offene Transaktion den Sequenzierer schon aufhält, auch aus einer anderen Datenbank desselben PostgreSQL-Clusters; 0 = nichts aufgehalten. Alarm ab 300 s (`apps/events/metrics.py`, beim Abruf aus der Datenbank gemessen) |
| `mandari_events_sequencer_lag_seconds` | – | Rückstand des Sequenzierers: Alter (ab Erfassung) des ältesten Ereignisses, das eine Folgenummer bekommen könnte, aber noch keine hat; 0 = kein Rückstand. Wächst, wenn kein Sequenzierer läuft oder er hängt – das zeigt `…_blocked_seconds` nicht. Direkt nach dem Commit einer langen Transaktion kurz hoch, Alarme deshalb mit Mindestdauer. Läuft kein Worker mit der Rolle `sequencer` (`manage.py events_worker`), wächst der Wert, sobald Ereignisse geschrieben werden |
| `mandari_events_oldest_transaction_seconds` | – | Alter der ältesten offenen Transaktion mit Transaktionskennung im Cluster, soweit die Datenbankrolle sie sehen darf |
| `mandari_events_sequenced_total` | – | vergebene Folgenummern; nur im Prozess des Sequenzierers (`events_worker` bzw. `events_sequencer`) |
| `mandari_events_published_total` | `type` | veröffentlichte Ereignisse je Typ, gezählt beim Vergeben der Folgenummer. So zählt jedes festgeschriebene Ereignis genau einmal, auch die des Ingestors (dessen eigene Zählung: `mandari_ingestor_events_published_total`). Die Summe über alle Typen entspricht `…_sequenced_total`; nur im Prozess des Sequenzierers |
| `mandari_events_listener_up` | – | 1, solange der Weckruf per `LISTEN` ankommt (Selbstprüfung alle 30 s), 0 bei Rückfall auf reine Abfrage, etwa hinter PgBouncer ohne `EVENTS_DB_DIRECT_URL`; nur in Prozessen mit Weckruf (`events_worker` mit `sequencer` oder `dispatch`, `events_sequencer`, `events_dispatch`) |
| `mandari_events_parked` | `subscription`, `state` (`wiederholen`, `blockiert`, `tot`) | geparkte Ereignisse der Zustellung je Abonnement, beim Abruf aus `events_parked` gezählt. `tot` = nach acht Versuchen aufgegeben; Alarm bei `tot` > 0. `blockiert` = Folgeereignisse eines Objekts, das auf ein geparktes Ereignis wartet |
| `mandari_events_lag_seconds` | `subscription` | Rückstand eines Abonnements: Alter (ab Erfassung) des ältesten nummerierten Ereignisses hinter seinem Cursor, das es zugestellt bekommt; 0 = aktuell. Nur für Abonnements, die im Code registriert sind (eine Zeile ohne Handler bekommt nie wieder etwas zugestellt). Beim Abruf aus der Datenbank gemessen (`apps/events/metrics.py`); Alarm ab 300 s, außer das Abonnement ist pausiert |
| `mandari_events_subscription_paused` | `subscription` | 1, solange ein Abonnement pausiert ist (Admin-Seite „Ereignistechnik → Abonnements“); sein Rückstand wächst dann bewusst |
| `mandari_events_delivered_total`, `mandari_events_delivery_failures_total`, `mandari_events_dead_total` | `subscription` | zugestellte Ereignisse, gescheiterte Zustellversuche und tot gewordene Ereignisse; nur im Prozess der Zustellung (`events_worker` bzw. `events_dispatch`) |
| `mandari_tasks_queued` | `queue` | Aufträge (`events_task`, Backend `JournalBackend`): fällige wartende Aufträge je Warteschlange, also der Rückstand des Runners (`events_worker` mit der Rolle `tasks` bzw. `events_tasks`; `apps/events/task_metrics.py`, beim Abruf aus der Datenbank gemessen) |
| `mandari_tasks_oldest_queued_seconds` | `queue` | wie lange der älteste fällige Auftrag schon wartet; wächst, wenn kein Runner läuft |
| `mandari_tasks_running` | `queue` | laufende Aufträge |
| `mandari_tasks_dead` | `queue` | tote (alle Versuche gescheitert) und endgültig fehlgeschlagene Aufträge, die in den letzten 24 Stunden beendet wurden; Alarm bei mehr als null (erlischt nach einem Tag von selbst), die Ursache steht im Protokoll des Runners |
| `mandari_tasks_duration_seconds` | `queue` | Laufzeit je Auftragsversuch; nur im Prozess des Runners |
| `mandari_tasks_failed_total` | `queue`, `grund` | gescheiterte Versuche: `fehler` (wird wiederholt), `endgueltig`, `zeitgrenze`, `sperre_abgelaufen` (Runner abgestürzt); nur im Prozess des Runners |
| `mandari_worker_rss_bytes` | `role` | belegter Arbeitsspeicher des Runners (`role="tasks"`); oberhalb von `TASKS_MAX_MEMORY_MB` startet er neu; nur im Prozess des Runners |
| `mandari_worker_role_up` | `role` | 1, solange die Rolle im Worker arbeitet (Faden lebt und hat sich innerhalb von `--stale-after`, Standard 300 s, gemeldet), sonst 0; nur im Worker (`manage.py events_worker`) |
| `mandari_worker_role_beat_age_seconds` | `role` | Sekunden seit dem letzten Lebenszeichen der Rolle, bei `dispatch` das älteste ihrer Abonnements; nur im Worker |

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

**Worker:** `manage.py events_worker` liefert die Metriken seines Prozesses (Runner, Zustellung,
Sequenzierer, Rollen) auf einem eigenen Port, Standard 9091 (`--metrics-port`), unter `/metrics`
mit denselben Zugriffsregeln, und unter `/health` (ohne Zugriffsbeschränkung, nur Rollen und
Zustand) 200 bzw. 503, wenn eine Rolle hängt. Vorschläge für Alarme:

```promql
# Rolle ausgefallen oder hängt (Heartbeat des Workers fehlt)
min by (role) (mandari_worker_role_up) == 0
absent(mandari_worker_role_up{role="tasks"})
# Aufträge scheitern an der Zeitgrenze oder ihr Runner ist abgestürzt
increase(mandari_tasks_failed_total{grund=~"zeitgrenze|sperre_abgelaufen"}[1h]) > 0
# Tote Aufträge bzw. Ereignisse, Rückstand
max by (queue) (mandari_tasks_dead) > 0
max by (queue) (mandari_tasks_oldest_queued_seconds) > 300
```

Die Werte, die beim Abruf aus der Datenbank gemessen werden (`mandari_tasks_queued`,
`mandari_events_parked`, `mandari_events_lag_seconds`, …), liefern Anwendung und Worker
gleichermaßen; Alarme fassen sie deshalb mit `max by (…)` zusammen, wie die Regeln in
`deploy/monitoring/prometheus-alerts.example.yml`.

Beispiel-Scrape-Konfiguration: `deploy/monitoring/prometheus-scrape.example.yml`;
Grafana-Vorlage (p95-Latenz je View, Fehlerquote, Pool-Belegung, Cache-Trefferquote):
`deploy/monitoring/grafana-mandari.json`. Der Ingestor liefert seine eigenen Metriken
(`mandari_ingestor_*`) weiterhin über seinen Port, darunter
`mandari_ingestor_events_published_total{type, source}`: ins Journal geschriebene Ereignisse je Typ
und Kommune (`source` wie bei `mandari_ingestor_entities_synced_total`), sobald
`INGESTOR_EVENTS_ENABLED` eingeschaltet ist (gezählt beim Schreiben, vor dem Commit). Bleibt der
Wert einer Kommune nach Vollabgleichen ohne Änderung hoch, liefert ihre Quelle eingebettete und
einzeln abgerufene Objekte unterschiedlich; `sync_config["events_enabled"] = false` an der Quelle
nimmt nur sie von den Ereignissen aus.

### Alarmregeln (Prometheus)

Fertige Regeln: `deploy/monitoring/prometheus-alerts.example.yml` (in Prometheus per `rule_files`
einbinden, Empfänger über Alertmanager). Schwellen und Begründung:

| Alarm | Ausdruck (gekürzt) | Dauer | Bedeutung, erster Schritt |
|---|---|---|---|
| Rückstand eines Abonnements | `mandari_events_lag_seconds > 300 unless on (subscription) mandari_events_subscription_paused == 1` | 5 min | Die Zustellung läuft nicht oder kommt nicht nach. Admin-Seite „Abonnements“, Protokoll von `events_dispatch` |
| Tote Ereignisse | `mandari_events_parked{state="tot"} > 0` | sofort | Ein Ereignis wurde nach acht Versuchen aufgegeben; seine Folgeereignisse warten. Admin-Seite „Geparkte Ereignisse“: Ursache beheben, dann erneut versuchen oder verwerfen. Die Mail über `check_service_levels` geht zusätzlich |
| Viele blockierte Ereignisse | `mandari_events_parked{state="blockiert"} > 1000` | 15 min | Hinter einem geparkten Ereignis eines häufig geänderten Objekts stauen sich Folgeereignisse (ohne Obergrenze, die Reihenfolge je Objekt bleibt erhalten). Hinweis, kein Notfall |
| Sequenzierer aufgehalten | `mandari_events_sequencer_blocked_seconds > 300` | 5 min | Eine lange offene Transaktion (auch einer anderen Datenbank im Cluster) hält die Vergabe auf. `pg_stat_activity` nach der ältesten Transaktion durchsehen |
| Sequenzierer im Rückstand | `mandari_events_sequencer_lag_seconds > 300` | 5 min | Kein Sequenzierer läuft oder er hängt; ohne Folgenummer stellt niemand zu |
| Tote Aufträge | `mandari_tasks_dead > 0` | sofort | Ein Auftrag ist endgültig gescheitert; Ursache im Protokoll des Runners |
| Auftragsrückstand | `mandari_tasks_oldest_queued_seconds > 900` | 10 min | Kein Runner bedient die Warteschlange oder sie kommt nicht nach |
| Weckruf gestört | `mandari_events_listener_up == 0` | 15 min | Nur Hinweis: Die Abfrage alle paar Sekunden trägt weiter, die Zustellung ist nur langsamer |

Die Dauer fängt kurze Spitzen ab: Direkt nach dem Commit einer langen Transaktion sind Rückstand und
Sequenzierer-Rückstand kurz hoch, ohne dass etwas klemmt. Die Werte der Ereignistechnik misst jeder
Webprozess beim Abruf aus der Datenbank; bei mehreren Instanzen in Abfragen `max` statt `sum` nehmen.

### Admin-Seite „Ereignistechnik“

Nur für Administratoren (Superuser); Eingriffe stehen im Sicherheitsprotokoll (Ereignis „Eingriff in
den Betrieb“, mit Konto, Adresse, Aktion und Kennungen, ohne Inhalte):

- **Abonnements:** Zustand, Warteschlange, Cursor, Rückstand (wie `mandari_events_lag_seconds`, rot ab
  300 s) und geparkte Ereignisse je Zustand. Aktionen: Pausieren (nichts mehr zustellen, der Cursor
  bleibt stehen), Fortsetzen (aktiv) und Fortsetzen im Schattenbetrieb – beide nur für pausierte.
- **Geparkte Ereignisse:** standardmäßig der Kopf jeder Kette je Objekt (wiederholen oder tot) mit der
  Zahl seiner Folgeereignisse; „Alle Ereignisse“ bzw. der Filter nach Zustand zeigt auch die
  blockierten. Aktionen: Erneut versuchen (nur das erste Ereignis eines Objekts, mit allen Versuchen) und
  Verwerfen (mit Bestätigung; das nächste Ereignis desselben Objekts rückt nach und wird sofort
  zugestellt).
- **Aufträge:** nur lesend, Filter nach Status und Warteschlange.

Dieselben Eingriffe gibt es auf der Kommandozeile (`manage.py events_dispatch --list`,
`--retry-parked`, `--discard-parked`); dort ohne Eintrag im Sicherheitsprotokoll.

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
| Tote Ereignisse | keine; ein Alarm je Abonnement mit Fehlerklassen. Die Zustellung löst dieselbe Prüfung sofort aus, wenn ein Ereignis tot wird (gemeinsame 24-h-Sperre) | `events_parked` (`manage.py events_dispatch --list`, danach `--retry-parked` bzw. `--discard-parked`) |

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
