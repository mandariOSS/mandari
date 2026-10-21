# Mehr-Server-Betrieb: Compose-Rollen data, web, worker

Stand: 20.10.2026 · Issues #55, #1107

mandari läuft standardmäßig auf **einem** Server: `docker compose up -d` startet alle Dienste
aus `docker-compose.yml`. Für größere Installationen (oder um Datenhaltung, Web-Auslieferung
und Hintergrundarbeit zu trennen) lassen sich dieselben Images ohne Codeänderung auf mehrere
Server verteilen. Dafür gibt es drei Rollenprofile als Compose-Overrides unter `deploy/roles/`:

| Rolle    | Dienste                                        | Datei                     |
|----------|------------------------------------------------|---------------------------|
| `data`   | PostgreSQL, Redis, Elasticsearch, optional PgBouncer | `deploy/roles/data.yml`   |
| `web`    | Caddy, Anwendung (`mandari`), Website          | `deploy/roles/web.yml`    |
| `worker` | Ingestor (Quellen-Sync), Protokoll-Orchestrator, Worker für Ereignisse, Aufträge und Zeitpläne (`worker`, `worker-heavy`) | `deploy/roles/worker.yml` |

Die Basisdatei bleibt unverändert und profilfrei; die Rollen-Dateien schalten die jeweils
fremden Dienste über ein nie aktiviertes Profil `aus` ab und biegen die Verbindungs-URLs
auf den Datenserver um. `scripts/check_compose_roles.py` prüft in der CI, dass jeder Dienst
genau einer Rolle zugeordnet ist.

## Voraussetzungen

- **Privates Netz** zwischen den Servern (VLAN, WireGuard, Cloud-VPC). Die Datendienste
  werden ausschließlich an die private Adresse `DATA_BIND` gebunden. Niemals an eine
  öffentliche Adresse – PostgreSQL, Redis und Elasticsearch sind ohne TLS und mit einfachem
  Passwort- bzw. ohne Schutz konfiguriert.
- **Gemeinsame Ablagen** auf allen Servern mit `web`- oder `worker`-Rolle: Die Volumes
  `mandari_media` (Uploads) und `mandari_files` (Dokument-Cache, `docs/FILE_CACHE.md`) müssen
  dort denselben Inhalt zeigen, z. B. als NFS-Mount. Das gilt auch bei nur einem web-Server: Der
  Worker erzeugt Sitzungsmappen aus hochgeladenen Unterlagen und füllt Personenfotos und
  Dokument-Cache; ohne gemeinsame Ablage scheitern die Mappen, und die Anwendung sieht Fotos und
  Cache nicht.
- **Dieselbe `.env`-Basis** auf allen Servern: `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
  `SECRET_KEY`, `ENCRYPTION_MASTER_KEY`, `DOMAIN` müssen überall identisch sein.

## Einrichtung

Auf jedem Server das Repository-Verzeichnis wie bei der Ein-Server-Installation anlegen
(`install.sh` oder manuell, siehe `DEPLOYMENT.md`), dann in der `.env` die Rolle wählen.
Compose liest `COMPOSE_FILE` aus der `.env`; `docker compose up -d`, `update.sh` und
`deploy/scripts/deploy.sh` funktionieren danach unverändert. Die Rollen-Dateien brauchen Docker
Compose ab 2.24.4 (`!reset`, `!override`).

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
DATA_PG_PORT=5432                # 6432, wenn dort PgBouncer aktiv ist, dann auch:
# MANDARI_DB_PGBOUNCER=true      # Anwendung auf den Transaktionsmodus einstellen
```

**Worker-Server:**

```ini
COMPOSE_FILE=docker-compose.yml:deploy/roles/worker.yml
DATA_HOST=10.0.0.10              # alle Dienste dieser Rolle direkt auf Port 5432
```

Reihenfolge beim ersten Start: data → web (führt die Migrationen aus) → worker. Prüfen mit
`docker compose config --services` (zeigt nur die Dienste der Rolle) und
`docker compose ps`.

## Kein Doppellauf zeitgesteuerter Jobs

Mehrere Server bedeuten die Gefahr, dass derselbe Job zweimal läuft – etwa wenn das
`worker`-Profil versehentlich auf zwei Servern aktiv ist oder auf einem web-Server noch ein alter
Cron-Eintrag steht. Drei Schutzmechanismen:

1. **Management-Commands** (Zeitpläne im Worker, `DEPLOYMENT.md` → „Geplante Aufgaben“;
   je Termin plant genau ein Scheduler über seine Lease einen Auftrag) tragen zusätzlich die
   Singleton-Sperre `apps/common/einmalig.py`: `cache.add` in Redis vergibt den Zuschlag
   atomar an genau einen Aufrufer; die Sperre verfällt nach `sperre_ttl` Sekunden von
   selbst (Absturzschutz) und wird nach dem Lauf sofort freigegeben. Ein zweiter Aufruf
   meldet `läuft bereits auf <host:pid> – übersprungen` und endet mit Exit-Code 0.
   Geschützt: `send_session_reminders`, `send_question_reminders`,
   `send_task_due_reminders`, `fetch_person_photos`, `cleanup_orphaned_accounts`,
   `check_source_health`, `check_service_levels`, `availability_report`,
   `build_meeting_packages`, `session_privacy_purge`, `cache_files`, `generate_alerts`,
   `send_digest` und die übrigen Befehle der Zeitpläne.
   `--ohne-sperre` erzwingt den Lauf (Notfall). Die Sperre ist das Sicherheitsnetz, die
   Lease des Schedulers das Konzept; Host-Cron für diese Befehle gibt es nicht mehr.
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

# Auf einem web-Server: denselben Befehl zweimal gleichzeitig von Hand starten
docker compose exec -T mandari python manage.py check_source_health --trotz-zeitplan & \
docker compose exec -T mandari python manage.py check_source_health --trotz-zeitplan
# → einer läuft, der andere meldet „läuft bereits auf … – übersprungen“
```

## Texterkennung auf einem eigenen Server

Die Texterkennung der RIS-Dateien (`TEXT_EXTRACTION_RUNNER=worker`, `DEPLOYMENT.md`, „Dokumentkette einschalten“)
braucht vor allem Rechenzeit: die erste Stufe `text` (nur Textebene, Dienst `worker`) einen Kern je Platz, die
Erkennung aus dem Bild `ocr` (Dienst `worker-heavy`) zusätzlich Speicher für Tesseract. Beide lesen nur aus der
Dokumentablage oder dem Objektspeicher, nie bei der Quelle (Issues #919, #1107). Ein zusätzlicher Server übernimmt
deshalb einen Teil der Erkennung, ohne die Ablage des Anwendungsservers einzuhängen, wenn der Objektspeicher
eingeschaltet ist und der Bestand darin liegt (Voraussetzungen unten):

```ini
# .env des Erkennungsservers (dieselbe Basis wie auf den übrigen Servern)
COMPOSE_FILE=docker-compose.yml:deploy/roles/worker.yml
DATA_HOST=10.0.0.10
TASKS_BACKEND=journal
TEXT_EXTRACTION_RUNNER=worker
OBJ_ENABLED=true                 # dieselben OBJ_*-Werte wie Anwendung und Worker
OBJ_CACHE_MAX_GB=20              # lokaler Zwischenspeicher dieses Servers
FILE_STORE_SHARED=false          # sieht die Ablage des Anwendungsservers nicht
```

**Voraussetzungen.** Vor dem ersten Start ist der Bestand umgestellt und hochgeladen: `dokumentablage --umstellen`
und `dokumentablage --hochladen` (je mit `--trotz-zeitplan`, `docs/FILE_CACHE.md`, „Objektspeicher“) melden nichts
mehr offen. Prüfen im Container `mandari` des Anwendungsservers, beide Zahlen 0:

```bash
docker compose exec -T mandari python manage.py shell -c "
from insight_core.models import OParlFile, OParlFileBlob
print('nicht hochgeladen', OParlFileBlob.objects.filter(remote_at__isnull=True, ref_count__gt=0).count())
print('altes Layout', OParlFile.objects.filter(local_status='ok', blob__isnull=True).count())"
```

`FILE_STORE_SHARED=false` ist Pflicht. Damit bestätigt der Erkennungsserver nie, dass ein Inhalt fehlt: Was weder in
seinem Zwischenspeicher noch im Objektspeicher liegt, gilt als gestört. Der Auftrag gibt die Datei frei und versucht
es nach 15 Minuten erneut, ohne Abruf bei der Quelle; das Protokoll von `worker-heavy` warnt höchstens einmal je
Minute. Ohne die Einstellung hielte er solche Inhalte für verloren, setzte sie auf „nicht abgelegt“ zurück, und der
Abrufweg holte sie erneut bei der Quelle, auch bei Quellen, die unsere Abrufe sperren. Neue Inhalte lädt der Abruf
sofort in den Objektspeicher; scheitert das, holt das stündliche Hochladen sie nach. Bis dahin warten sie hier, oder
ein Worker des Anwendungsservers erkennt sie.

```bash
docker compose up -d worker-heavy        # nur die Erkennung, nicht die übrigen Dienste der Rolle
```

- **Keine doppelte Arbeit:** Mehrere `worker-heavy` (auf einem oder mehreren Servern) teilen sich die
  Warteschlange `ocr`. Jeder Runner holt einen Auftrag nur einmal (`SELECT … FOR UPDATE SKIP LOCKED`), und der
  Auftrag beansprucht seine Datei beim Start; ein zweiter Auftrag derselben Datei endet ohne Wirkung. Gespeichert
  wird nur, solange der Inhalt noch derselbe ist.
- **Die erste Stufe auslagern:** ebenso ein Dienst wie `worker-heavy` mit `--roles tasks --queues text
  --concurrency text=N` (N höchstens die Zahl der Kerne), etwa in einer eigenen Compose-Datei dieses Servers. Er teilt
  sich die Warteschlange mit dem Hauptworker; wer ihn dort entlasten will, nimmt `text` aus dessen `--queues`. Der
  Zeitplan füllt `text` nur, solange irgendein lebender Worker sie bedient.
- **Lokale Kopien begrenzen:** Inhalte aus dem Objektspeicher landen im Volume `mandari_files` dieses Servers. Das
  stündliche Aufräumen (`befehl:dokumentablage`) läuft nur im Worker mit Scheduler und sieht diese Platte nicht. Auf
  dem Erkennungsserver deshalb per Host-Cron stündlich, versetzt zum Zeitplan (:50):
  `docker compose exec -T worker-heavy python manage.py dokumentablage --aufraeumen --trotz-zeitplan`. Er verdrängt
  nur Kopien, die nach `HEAD` sicher im Objektspeicher liegen, und lässt Inhalte stehen, deren Text gerade erkannt
  wird oder darauf wartet; dieselbe Sperre in Redis verhindert zwei Läufe zugleich. Ohne diesen Lauf füllt sich die
  Platte bis `FILE_CACHE_MIN_FREE_GB`, danach stellt die Erkennung ihre Aufträge immer wieder zurück.
- **Ohne Objektspeicher** braucht der Erkennungsserver die gemeinsame Ablage `mandari_files` wie jeder Server mit
  `worker`-Rolle (Voraussetzungen oben), und `FILE_STORE_SHARED` bleibt `true`. Ohne gemeinsame Ablage findet die
  Erkennung keine Inhalte und gibt die Dateien an den Abruf zurück.
- **Updates:** `update.sh` startet alle Dienste der Rolle `worker`. Auf dem Erkennungsserver stattdessen
  `docker compose pull worker-heavy && docker compose up -d worker-heavy`, nach den Migrationen auf dem
  Anwendungsserver.

## Prozesslokale Zustände

Rate-Limits (Anmeldung), Alarm-Sperren, CSP-Report-Limit und die Singleton-Sperren liegen
im Django-Cache und damit in Redis. Prozesslokal bleiben nur unveränderliche, aus Settings
abgeleitete Werte (`lru_cache` für Netzlisten) und die Prometheus-Zähler je Prozess – die
Metriken sind je web-Server auszulesen (`docs/MONITORING.md`).

## PgBouncer (optional)

`COMPOSE_PROFILES=pgbouncer` auf dem Datenserver startet PgBouncer im Transaktionsmodus
(Port 6432 an `DATA_BIND`). Über ihn geht nur die Anwendung auf den web-Servern: dort
`DATA_PG_PORT=6432` **und** `MANDARI_DB_PGBOUNCER=true` (Issue #1060). Der Schalter stellt die
Anwendung auf den Transaktionsmodus ein (keine serverseitigen Cursor für `.iterator()`, keine
vorbereiteten Anweisungen); ihre Migrationen verbinden sich über `DB_DIRECT_URL` direkt.
Die Anwendung nutzt bereits einen psycopg-Verbindungspool (#257); PgBouncer lohnt sich ab
mehreren web-Servern, damit PostgreSQL nicht mit `max_connections` kollidiert. Im
Transaktionsmodus sind sitzungsgebundene Funktionen (z. B. `SET` ohne `LOCAL`,
Advisory-Sperren, `LISTEN`) nicht nutzbar – Website, Ingestor, Orchestrator und Worker
verbinden sich deshalb **direkt** mit PostgreSQL (Port 5432); `DATA_PG_PORT` gilt nur für die
Anwendung. Verbindungsrechnung, Einschalten und Rückweg: `DEPLOYMENT.md`, „PgBouncer für die
Webanwendung“.

Auch `LISTEN` funktioniert im Transaktionsmodus nicht: Die Meldungen landen bei einer fremden
Serververbindung. Die Ereignistechnik (Sequenzierer und Zustellung, `apps/events`) hört deshalb
auf einer eigenen Direktverbindung, sobald `EVENTS_DB_DIRECT_URL` gesetzt ist, etwa
`postgresql://mandari:…@<DATA_HOST>:5432/mandari`; für den Dienst `worker` setzt `worker.yml` sie
selbst, obwohl dessen `DATABASE_URL` inzwischen ohnehin direkt verbindet. Zeigt `DATABASE_URL`
eines Workers doch auf einen Pooler, läuft alles andere – auch die Leader-Leases – darüber:
Leases sind Tabellenzeilen, die innerhalb einer Transaktion geprüft
werden, sitzungsgebundene Sperren gibt es dort nicht. Fehlt die Einstellung hinter PgBouncer,
erkennt der Listener das an seiner Selbstprüfung, protokolliert eine Warnung
(`mandari_events_listener_up` = 0), und Sequenzierer und Zustellung fragen nur regelmäßig ab;
Ereignisse kommen dann bis zu etwa drei Sekunden später an.

## Grenzen

- Kein automatisches Failover: Fällt der Datenserver aus, stehen alle Rollen. Backups
  laufen weiterhin auf dem Datenserver (`docs/BACKUP.md`).
- Die Kollaboration im Editor (WebSockets über Channels) braucht bei mehreren web-Servern
  den Redis-Channel-Layer (`REDIS_URL` gesetzt → Standard); der In-Memory-Layer gilt nur
  für Tests.
- Rollen auf Kubernetes: siehe `deploy/kubernetes/` (Helm) – dort sind die Rollen ohnehin
  getrennte Deployments.
