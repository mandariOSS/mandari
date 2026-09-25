# Mehr-Server-Betrieb: Compose-Rollen data, web, worker

Stand: 17.09.2026 · Issue #55

mandari läuft standardmäßig auf **einem** Server: `docker compose up -d` startet alle Dienste
aus `docker-compose.yml`. Für größere Installationen (oder um Datenhaltung, Web-Auslieferung
und Hintergrundarbeit zu trennen) lassen sich dieselben Images ohne Codeänderung auf mehrere
Server verteilen. Dafür gibt es drei Rollenprofile als Compose-Overrides unter `deploy/roles/`:

| Rolle    | Dienste                                        | Datei                     |
|----------|------------------------------------------------|---------------------------|
| `data`   | PostgreSQL, Redis, Elasticsearch, optional PgBouncer | `deploy/roles/data.yml`   |
| `web`    | Caddy, Anwendung (`mandari`), Website          | `deploy/roles/web.yml`    |
| `worker` | Ingestor (Quellen-Sync), Protokoll-Orchestrator | `deploy/roles/worker.yml` |

Die Basisdatei bleibt unverändert und profilfrei; die Rollen-Dateien schalten die jeweils
fremden Dienste über ein nie aktiviertes Profil `aus` ab und biegen die Verbindungs-URLs
auf den Datenserver um. `scripts/check_compose_roles.py` prüft in der CI, dass jeder Dienst
genau einer Rolle zugeordnet ist.

## Voraussetzungen

- **Privates Netz** zwischen den Servern (VLAN, WireGuard, Cloud-VPC). Die Datendienste
  werden ausschließlich an die private Adresse `DATA_BIND` gebunden. Niemals an eine
  öffentliche Adresse – PostgreSQL, Redis und Elasticsearch sind ohne TLS und mit einfachem
  Passwort- bzw. ohne Schutz konfiguriert.
- **Gemeinsame Ablagen** bei mehr als einem `web`-Server: Die Volumes `mandari_media`
  (Uploads) und `mandari_files` (Dokument-Cache, `docs/FILE_CACHE.md`) müssen auf allen
  web-Servern denselben Inhalt zeigen, z. B. als NFS-Mount. Mit nur einem web-Server entfällt das.
- **Dieselbe `.env`-Basis** auf allen Servern: `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
  `SECRET_KEY`, `ENCRYPTION_MASTER_KEY`, `DOMAIN` müssen überall identisch sein.

## Einrichtung

Auf jedem Server das Repository-Verzeichnis wie bei der Ein-Server-Installation anlegen
(`install.sh` oder manuell, siehe `DEPLOYMENT.md`), dann in der `.env` die Rolle wählen.
Compose liest `COMPOSE_FILE` aus der `.env`; `docker compose up -d`, `update.sh` und
`deploy/scripts/deploy.sh` funktionieren danach unverändert.

**Datenserver:**

```ini
COMPOSE_FILE=docker-compose.yml:deploy/roles/data.yml
DATA_BIND=10.0.0.10              # private Adresse dieses Servers
# optional: Verbindungs-Pooler
# COMPOSE_PROFILES=pgbouncer
```

**Web-Server:**

```ini
COMPOSE_FILE=docker-compose.yml:deploy/roles/web.yml
DATA_HOST=10.0.0.10              # private Adresse des Datenservers
DATA_PG_PORT=5432                # 6432, wenn dort PgBouncer aktiv ist
```

**Worker-Server:**

```ini
COMPOSE_FILE=docker-compose.yml:deploy/roles/worker.yml
DATA_HOST=10.0.0.10
DATA_PG_PORT=5432
```

Reihenfolge beim ersten Start: data → web (führt die Migrationen aus) → worker. Prüfen mit
`docker compose config --services` (zeigt nur die Dienste der Rolle) und
`docker compose ps`.

## Kein Doppellauf zeitgesteuerter Jobs

Mehrere Server bedeuten die Gefahr, dass derselbe Job zweimal läuft – etwa wenn das
`worker`-Profil versehentlich auf zwei Servern aktiv ist oder Cron auf zwei web-Servern
eingerichtet wurde. Drei Schutzmechanismen:

1. **Management-Commands** (Cron, `DEPLOYMENT.md` → „Geplante Aufgaben“) tragen die
   Singleton-Sperre `apps/common/einmalig.py`: `cache.add` in Redis vergibt den Zuschlag
   atomar an genau einen Aufrufer; die Sperre verfällt nach `sperre_ttl` Sekunden von
   selbst (Absturzschutz) und wird nach dem Lauf sofort freigegeben. Ein zweiter Aufruf
   meldet `läuft bereits auf <host:pid> – übersprungen` und endet mit Exit-Code 0.
   Geschützt: `send_session_reminders`, `send_question_reminders`,
   `send_task_due_reminders`, `fetch_person_photos`, `cleanup_orphaned_accounts`,
   `check_source_health`, `check_service_levels`, `availability_report`,
   `build_meeting_packages`.
   `--ohne-sperre` erzwingt den Lauf (Notfall). Cron trotzdem nur auf **einem** Server
   einrichten – die Sperre ist das Sicherheitsnetz, nicht das Konzept.
2. **Protokoll-Orchestrator** (`minutes_orchestrator`) hält dieselbe Sperre je Durchlauf;
   ein zweiter Orchestrator überspringt Takte statt doppelt Rechenknoten anzulegen.
3. **Ingestor-Daemon** nutzt eine PostgreSQL-Advisory-Sperre (`pg_try_advisory_lock`),
   die an seine Datenbankverbindung gebunden ist: Nur eine Instanz synchronisiert, eine
   zweite wartet (`Andere Ingestor-Instanz aktiv … – warte`) und übernimmt automatisch,
   sobald die erste endet. Kein veralteter Schlüssel, kein Aufräumen nach Abstürzen.

Voraussetzung für 1 und 2 ist der gemeinsame Redis-Cache (`REDIS_URL`); mit dem lokalen
Speicher-Cache (Entwicklung) schützt die Sperre nur innerhalb eines Prozesses.

### Nachweis: zwei worker-Profile erzeugen keine doppelten Läufe

```bash
# Auf dem Datenserver: wer hält die Ingestor-Sperre?
docker compose exec postgres psql -U mandari -c \
  "SELECT application_name, state FROM pg_stat_activity WHERE application_name LIKE 'ingestor-daemon %';"
# → genau eine Zeile, auch wenn zwei Ingestor-Container laufen

# Auf einem web-Server: Cron-Command zweimal gleichzeitig starten
docker compose exec -T mandari python manage.py check_source_health & \
docker compose exec -T mandari python manage.py check_source_health
# → einer läuft, der andere meldet „läuft bereits auf … – übersprungen“
```

## Prozesslokale Zustände

Rate-Limits (Anmeldung), Alarm-Sperren, CSP-Report-Limit und die Singleton-Sperren liegen
im Django-Cache und damit in Redis. Prozesslokal bleiben nur unveränderliche, aus Settings
abgeleitete Werte (`lru_cache` für Netzlisten) und die Prometheus-Zähler je Prozess – die
Metriken sind je web-Server auszulesen (`docs/MONITORING.md`).

## PgBouncer (optional)

`COMPOSE_PROFILES=pgbouncer` auf dem Datenserver startet PgBouncer im Transaktionsmodus
(Port 6432 an `DATA_BIND`). Web- und Worker-Server zeigen dann mit `DATA_PG_PORT=6432`
auf den Pooler. Die Anwendung nutzt bereits einen psycopg-Verbindungspool (#257); PgBouncer
lohnt sich ab mehreren web-Servern, damit PostgreSQL nicht mit `max_connections`
kollidiert. Im Transaktionsmodus sind sitzungsgebundene Funktionen (z. B. `SET` ohne
`LOCAL`, Advisory-Sperren) nicht nutzbar – der Ingestor verbindet sich deshalb in
`worker.yml` **direkt** mit PostgreSQL (Port 5432); `DATA_PG_PORT` gilt für Anwendung,
Website und Orchestrator.

## Grenzen

- Kein automatisches Failover: Fällt der Datenserver aus, stehen alle Rollen. Backups
  laufen weiterhin auf dem Datenserver (`docs/BACKUP.md`).
- Die Kollaboration im Editor (WebSockets über Channels) braucht bei mehreren web-Servern
  den Redis-Channel-Layer (`REDIS_URL` gesetzt → Standard); der In-Memory-Layer gilt nur
  für Tests.
- Rollen auf Kubernetes: siehe `deploy/kubernetes/` (Helm) – dort sind die Rollen ohnehin
  getrennte Deployments.
