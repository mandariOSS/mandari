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
| Kritisch | älter als `INSIGHT_SOURCE_STALE_CRITICAL_DAYS` (Standard 7 Tage) **oder** ≥ 3 Fehlversuche in Folge **oder** seit > 7 Tagen angelegt und nie synchronisiert |
| Inaktiv | `is_active = false` — erscheint nur im Admin-Filter der Quellenliste, nicht im Betriebsmonitor |

Der **Ingestor** schreibt bei jedem fehlgeschlagenen Versuch `last_error`, `last_error_at` und
zählt `consecutive_failures` hoch (`storage.record_source_failure`); ein erfolgreicher Sync setzt
die Werte zurück. So ist der konkrete Grund (z. B. `HTTP 403`) direkt im Admin sichtbar.

Ab `INSIGHT_SOURCE_BACKOFF_FAILURES` (Standard 3) Fehlversuchen greift die **Quellen-Schonung**:
Dokument-Cache und Datei-Proxy pausieren für diese Quelle, der Ingestor verdoppelt den Abstand
zwischen den Versuchen bis auf 6 Stunden (siehe `docs/FILE_CACHE.md`).

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
