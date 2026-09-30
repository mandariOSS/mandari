# Mandari 2.0 - Deployment Guide

## Übersicht

| Weg | Wann nutzen |
|-----|-------------|
| `install.sh` | Erstinstallation auf einem Server mit Docker Compose (siehe [README](README.md#installation)) |
| `update.sh` | Aktualisieren für Selbstbetreiber: Sicherung, Migration, Umschalten, Prüfung, Rückfall |
| `deploy/scripts/deploy.sh` | Nicht interaktiv (Betrieb, Cron, CI), gleiche Prüfung und gleicher Rückfall |
| `install-k8s.sh` bzw. Helm | Kubernetes, siehe [`deploy/kubernetes/README.md`](deploy/kubernetes/README.md) |

Die Images baut `.github/workflows/release.yml` und legt sie in der GitHub Container Registry ab
(`ghcr.io/mandarioss/mandari`, `ghcr.io/mandarioss/ingestor`): `dev` → `:dev`, `main` → `:latest`,
ein Release → `:<version>` und `:latest`. Ein Push deployt nichts; umgeschaltet wird auf dem Server
mit `update.sh` oder `deploy/scripts/deploy.sh`.

### Installation ohne Rückfragen

`./install.sh --unattended` übernimmt Domain, Admin-Konto und weitere Werte aus Umgebungsvariablen
(Beispiel im [README](README.md#installation)) und ist für Automatisierung gedacht. Er richtet nur
eine **neue** Installation ein: Liegt im Verzeichnis bereits eine `.env`, bricht er mit Exit-Code 1
ab, ohne Container, Daten oder Konfiguration anzufassen. Eine bestehende Installation wird mit
`./update.sh` aktualisiert.

Soll eine Installation ohne Rückfragen verworfen und neu aufgesetzt werden (z. B. eine
Testumgebung), muss das ausdrücklich angegeben werden:

```bash
./backup.sh                                          # vorher sichern
./install.sh --unattended --reinstall-destroy-data
```

Das entfernt die Container und **alle Daten-Volumes** (Datenbank, Uploads, Dokument-Cache,
Suchindex, Zertifikate) und erzeugt neue Schlüssel. Die bisherige `.env` bleibt als
`.env.vor-neuinstallation-<Zeitstempel>` liegen: Sie enthält den alten `ENCRYPTION_MASTER_KEY`, ohne
den ältere Sicherungen nicht mehr lesbar sind. Die Datei nach Gebrauch sicher verwahren oder löschen.
Interaktiv (`./install.sh` ohne `--unattended`) fragt der Installer wie bisher nach.

`./install.sh --help` zeigt alle Optionen.

---

## 🚀 Aktualisieren

### Mit `update.sh`

```bash
./update.sh                # auf latest
./update.sh --tag v1.3.0   # bestimmte Version
./update.sh --dry-run      # nur prüfen, nichts ändern
./update.sh --rollback     # auf die vorherige Version zurück
```

### Mit Skript: Gesundheitsprüfung und automatischer Rückfall

`deploy/scripts/deploy.sh` ist nicht interaktiv (für Betrieb, Cron, CI) und macht aus
jedem Deploy einen geprüften Vorgang: Sicherung, `safemigrate`, Umschalten mit `--wait`,
dann **Anwendungsprüfung im Container** (`deploy/scripts/verify_deploy.py`: Readiness,
Anmeldeseite, Bürgerportal, OParl-System, optional angemeldete Demo-Seiten, jeweils mit
Inhaltsprüfung). Scheitert sie, schaltet das Skript **selbsttätig auf das vorherige Image
zurück** und meldet das per Mail. Jeder Lauf schreibt eine Zeile in `deploy-log.tsv`
(alt, neu, Ergebnis, Unterbrechung in Sekunden, Dauer), die Grundlage für die Kennzahl
„Ausfallzeit je Deploy“ aus dem Verfügbarkeitskonzept.

```bash
# Umgebung einmalig in einer Datei ablegen (Dienstnamen, Compose-Dateien, Empfänger)
set -a; . /opt/mandari/deploy.env; set +a
sh deploy/scripts/deploy.sh plan   v0.12.0   # Images ziehen, migrate --plan, check
sh deploy/scripts/deploy.sh apply  v0.12.0   # Sicherung, Migration, Umschalten, Prüfung, ggf. Rückfall
sh deploy/scripts/deploy.sh verify           # nur die Prüfung gegen den laufenden Stand
sh deploy/scripts/deploy.sh rollback v0.11.0 # von Hand zurück
```

Alle Parameter (`MANDARI_DIR`, `COMPOSE_FILES`, `APP_SERVICE`, `WORKER_SERVICES`,
`DB_SERVICE`, `BACKUP_DIR`, `NOTIFY_EMAIL`, `VERIFY_*`) stehen im Kopf des Skripts.
Migrationen müssen abwärtskompatibel sein: Der Rückfall rollt Code zurück, keine
Migrationen (`django-safemigrate` spielt nur verträgliche Migrationen vor dem Umschalten ein).
Das interaktive `update.sh` für Selbst-Hoster nutzt dieselbe Anwendungsprüfung und rollt
bei Fehlschlag ebenfalls zurück. Wie `deploy.sh` hält es die Worker (`WORKER_SERVICES`,
Standard `ingestor minutes-orchestrator`) während aller Migrationen an und startet sie erst danach
mit dem neuen Image – auch bei Abbruch oder Rückfall werden sie wieder gestartet. Der
Protokoll-Orchestrator (`minutes-orchestrator`) nutzt das Anwendungs-Image und wechselt nach den
Migrationen immer mit, auch wenn ein eigenes `WORKER_SERVICES` ihn nicht nennt; `--rollback`
setzt ihn ebenfalls zurück. Dienste, die die Compose-Datei nicht kennt, überspringt das Skript.

Für `deploy.sh` gehört der Orchestrator ebenfalls in `WORKER_SERVICES`
(z. B. `WORKER_SERVICES="ingestor minutes-orchestrator"`); sonst läuft er nach dem Deploy mit dem
alten Image weiter.

---

## 📋 Deployment Checkliste

### Vor der Installation

- [ ] Linux-Server mit mindestens 4 GB Arbeitsspeicher (siehe [README](README.md#installation))
- [ ] Domain, deren DNS auf den Server zeigt

### Nach der Installation

- [ ] `https://<domain>/health/ready/` meldet `"status": "ok"`
- [ ] Tägliche Sicherung eingetragen (`crontab -l`), Probelauf mit `./backup.sh --verify`
- [ ] Passphrase für die Konfiguration in der Sicherung eingerichtet und außerhalb des Servers verwahrt
  (Abschnitt „Sicherung“)
- [ ] Geplante Aufgaben eingerichtet (Abschnitt „Geplante Aufgaben (Cron)“)

---

## 🔄 Typische Workflows

### Rollback

```bash
./update.sh --rollback                        # vorherige Version
sh deploy/scripts/deploy.sh rollback v0.11.0  # bestimmte Version
./backup.sh --restore <Sicherungsdatei>       # Daten aus einer Sicherung zurückspielen
```

### Datenbank-Migration

Migrationen laufen beim Update, nicht im Entrypoint: verträgliche vor dem Umschalten
(`safemigrate`), `update.sh` spielt die übrigen danach ein. Ingestor und Protokoll-Orchestrator
stehen dabei, damit kein Durchlauf gegen ein Schema läuft, das sich gerade ändert. Von Hand ebenso
(Containername folgt `COMPOSE_PROJECT_NAME`, Vorgabe `mandari`):

```bash
docker compose stop ingestor minutes-orchestrator
docker exec mandari python manage.py migrate
docker compose up -d ingestor minutes-orchestrator
```

### Sicherung

`./backup.sh` schreibt ein Archiv nach `./backups/` (die letzten sieben bleiben); `install.sh`
richtet dafür einen täglichen Cron-Lauf ein (`./backup.sh --quiet`). `./backup.sh --help` zeigt
alle Optionen. Verschlüsselte Sicherung an zwei Standorten mit restic: [docs/BACKUP.md](docs/BACKUP.md).

| Inhalt | Im Archiv | Abschalten |
|---|---|---|
| Datenbanken mandari und Website | `postgres.sql`, `postgres_website.sql` (`pg_dump`) | – |
| Uploads (`/app/media`, Volume `mandari_media`) | `media.tar` | `--no-media` bzw. `BACKUP_NO_MEDIA=true` |
| Dokument-Cache (`/app/files`, Volume `mandari_files`) | `files.tar` | `--no-files` bzw. `BACKUP_NO_FILES=true` |
| Konfiguration (`.env`) | `config.env.enc`, nur verschlüsselt | ohne Passphrase ausgelassen |
| Suchindex, Redis, TLS-Zertifikate | nicht enthalten | Index wird bei der Wiederherstellung neu aufgebaut, Caddy holt neue Zertifikate |

Die Datei-Volumes liest ein kurzlebiger Container des Anwendungsdienstes (`docker compose run`),
also mit denselben Volumes bzw. Bind-Mounts wie im Betrieb. Der Dokument-Cache kann je Kommune
mehrere Gigabyte groß werden und lässt sich aus den Ratsinformationssystemen neu laden; bei sehr
großen Ablagen `BACKUP_NO_FILES=true` in die `.env` schreiben (gilt dann auch für Cron und
`update.sh`). Während der Sicherung braucht das Zielverzeichnis Platz für die unkomprimierten
Bestandteile und das Archiv.

Ein unvollständiger Lauf (Uploads nicht lesbar, Passphrase-Datei fehlt …) schreibt das Archiv
trotzdem, endet aber mit Exit-Code 1 und räumt dann keine alten Archive auf.

#### Konfiguration nur verschlüsselt

Die `.env` enthält alle Schlüssel, darunter den `ENCRYPTION_MASTER_KEY`, ohne den verschlüsselte
Inhalte unlesbar sind. `backup.sh` legt sie deshalb nur verschlüsselt ins Archiv
(`openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256`) und prüft das Ergebnis durch
Zurück-Entschlüsseln. Die Passphrase kommt aus `BACKUP_PASSPHRASE` (nur Umgebung) oder aus der
ersten Zeile der Datei `BACKUP_PASSPHRASE_FILE` (Umgebung oder `.env`, Vorgabe
`./.backup-passphrase`). Ohne Passphrase fehlt die Konfiguration im Archiv, und jeder Lauf warnt.

Einrichtung, damit auch der Cron-Lauf und `update.sh` die Passphrase finden:

```bash
(umask 077; openssl rand -base64 32 > .backup-passphrase)
./backup.sh --verify
```

Die Passphrase **zusätzlich außerhalb des Servers** verwahren (Passwort-Manager): Für eine
Wiederherstellung auf einem neuen Server wird sie gebraucht, und auf dem Server liegt sie neben der
`.env`. Ein Archiv, das den Server verlässt (z. B. über `S3_BACKUP_BUCKET`), enthält so keine
Geheimnisse im Klartext. Datenbank und Uploads darin sind weiterhin personenbezogene Daten; das
Archiv wird nur für den Eigentümer lesbar angelegt. Archive früherer Versionen enthalten die `.env`
im Klartext und gehören entsprechend geschützt oder gelöscht.

Von Hand entschlüsseln (fragt nach der Passphrase):

```bash
tar -xzf mandari_backup_<zeit>.tar.gz mandari_backup_<zeit>/config.env.enc
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 \
  -in mandari_backup_<zeit>/config.env.enc -out env.aus-sicherung
```

#### Wiederherstellen

```bash
./backup.sh --restore backups/mandari_backup_<zeit>.tar.gz
# Passphrase aus ./.backup-passphrase oder z. B. BACKUP_PASSPHRASE_FILE=/pfad/zur/datei
```

Das Skript liest zuerst das ganze Archiv (Prüfsumme) und entschlüsselt die Konfiguration; scheitert
eins davon, bricht es ab, ohne etwas zu verändern. Nach Übersicht und Rückfrage:

1. Dienste stoppen und die Konfiguration aus der Sicherung einsetzen. Eine abweichende bisherige
   `.env` bleibt als `.env.vor-wiederherstellung-<zeit>` liegen; das Passwort der Datenbankrolle wird
   an die wiederhergestellte `.env` angeglichen.
2. Nur PostgreSQL starten, jede Datenbank in eine Zwischen-Datenbank einspielen und erst danach
   gegen die bestehende tauschen. Schlägt das Einspielen fehl, bleibt die bisherige Datenbank
   unverändert.
3. Uploads und Dokument-Cache zurückspielen (gleichnamige Dateien werden überschrieben, später
   hinzugekommene bleiben liegen).
4. Alle Dienste starten, `migrate` ausführen und den Suchindex neu aufbauen
   (`setup_elasticsearch`, `reindex_elasticsearch --clear`).

**Neuer Server:** Docker installieren, Repository klonen, Archiv und Passphrase bereitstellen und direkt
`./backup.sh --restore …` aufrufen, ohne vorher `install.sh` auszuführen – die Konfiguration kommt
aus der Sicherung. Danach die tägliche Sicherung wieder eintragen:

```cron
0 2 * * * cd /opt/mandari && ./backup.sh --quiet >> /opt/mandari/logs/backup.log 2>&1
```

Enthält die Sicherung keine Konfiguration, vorher die `.env` der alten Installation (mit demselben
`ENCRYPTION_MASTER_KEY`) ins Verzeichnis legen. Eine Wiederherstellung regelmäßig in einer
Testumgebung durchspielen – erst sie zeigt, dass die Sicherung taugt.

---

## 🐘 PostgreSQL: Grundeinstellungen

PostgreSQL liefert Werte aus, die zu einem kleinen Rechner mit drehender
Festplatte passen. Für mandari trifft beides nicht zu: `shared_buffers=128MB`
gegen einen Datenbestand von mehreren Gigabyte bedeutet, dass eine einzige
größere Abfrage den gesamten Cache verdrängt. `random_page_cost=4` beschreibt
eine Festplatte und führt dazu, dass der Planer Indizes meidet und stattdessen
ganze Tabellen liest.

Die mitgelieferte `docker-compose.yml` setzt deshalb eigene Werte. **Jeder ist
per Umgebungsvariable übersteuerbar**, und eine Änderung wirkt erst nach einem
Neustart des Datenbankdienstes.

### Woher die Werte kommen

Alles leitet sich vom Arbeitsspeicher des **Datenbankdienstes** ab
(`POSTGRES_MEM_LIMIT`, Vorgabe `1g`) — nicht vom Arbeitsspeicher des Servers.

| Einstellung | Faustregel | Vorgabe bei 1 GB |
|---|---|---|
| `POSTGRES_SHARED_BUFFERS` | **ein Viertel** des Dienst-Speichers | `256MB` |
| `POSTGRES_EFFECTIVE_CACHE_SIZE` | **drei Viertel** — eine Schätzung, keine Belegung: was der Planer an Cache erwarten darf | `768MB` |
| `POSTGRES_WORK_MEM` | je Sortier- oder Gruppiervorgang, **nicht je Verbindung** | `8MB` |
| `POSTGRES_MAINTENANCE_WORK_MEM` | für `VACUUM` und Indexaufbau, etwa ein Achtel | `128MB` |
| `POSTGRES_RANDOM_PAGE_COST` | `1.1` für SSD/NVMe, `4` nur für drehende Platten | `1.1` |
| `POSTGRES_EFFECTIVE_IO_CONCURRENCY` | `200` für SSD/NVMe, `2` für drehende Platten | `200` |
| `POSTGRES_MAX_CONNECTIONS` | muss zur Summe aller Dienste passen | `100` |
| `POSTGRES_MAX_WAL_SIZE` | mehr bedeutet seltenere Checkpoints | `2GB` |
| `POSTGRES_TEMP_FILE_LIMIT` | Obergrenze für temporäre Dateien **je Sitzung**; deutlich unter dem freien Plattenplatz | `5GB` |

**Die Falle bei `work_mem`:** Der Wert gilt je Sortiervorgang, und eine einzelne
Abfrage kann mehrere davon haben. Im Extremfall belegt die Datenbank
`max_connections × work_mem` zusätzlich zu `shared_buffers`. Bei 100 Verbindungen
und 8 MB sind das rechnerisch 800 MB — deshalb sind `mem_limit`,
`max_connections` und `work_mem` nur gemeinsam zu ändern.

**Warum `temp_file_limit`:** Sortier- und Gruppiervorgänge, die nicht in `work_mem`
passen, schreibt PostgreSQL in temporäre Dateien. Ohne Grenze kann eine einzige
entgleiste Abfrage die Platte füllen – am 24.09.2026 tat das ein Zähl-Join über
Straßen und Adressen (Kreuzprodukt). Mit Grenze bricht nur diese Abfrage ab. Der
Wert lässt sich ohne Neustart setzen: `ALTER SYSTEM SET temp_file_limit = '5GB';
SELECT pg_reload_conf();`

Größenempfehlungen je Größenklasse (klein, mittel, groß) mit Mengengerüst,
Verbindungsbudget und den Ergebnissen der Lasttests: [docs/LASTTESTS.md](docs/LASTTESTS.md).

### Für größere Installationen

Beispiel aus dem eigenen Betrieb (Datenbankdienst mit 4 GB, gemeinsam genutzt von
mandari, Website und Kundenportal):

```env
POSTGRES_MEM_LIMIT=4g
POSTGRES_SHARED_BUFFERS=1GB
POSTGRES_EFFECTIVE_CACHE_SIZE=3GB
POSTGRES_WORK_MEM=8MB
POSTGRES_MAINTENANCE_WORK_MEM=256MB
POSTGRES_MAX_CONNECTIONS=200
```

### Wirkung

Gemessen am eigenen Bestand (Köln, 42.247 Vorgänge), zusammen mit den Indizes
aus #256:

| Seite | vorher | nachher |
|---|---|---|
| Insight-Portal, extern gemessen | 204 ms | **46 ms** |
| Auswahlseite, serverseitig | 193 ms | **12 ms** |
| Kommunenseite Köln | 215 ms | **20 ms** |

Listen- und Detailseiten lagen vorher wie nachher bei 10–50 ms; sie sind durch
die Umstellung nicht langsamer geworden.

## 🖧 Mehrere Server (Rollen data / web / worker)

Ein Server ist der Standard. Für getrennte Daten-, Web- und Worker-Server gibt es
Compose-Rollenprofile unter `deploy/roles/`, gewählt über `COMPOSE_FILE` in der `.env`;
zeitgesteuerte Jobs sind gegen Doppelläufe gesperrt. Anleitung, Voraussetzungen (privates
Netz, gemeinsame Ablagen) und Nachweis: `docs/MEHR_SERVER_BETRIEB.md` (Issue #55).

## ⏰ Geplante Aufgaben (Cron)

Die Anwendung bringt keinen eigenen Scheduler mit. Wiederkehrende Management-Commands
laufen auf dem Host per Cron gegen den laufenden Container, jeweils mit eigener Logdatei.
Jedes dieser Commands hält während des Laufs eine Singleton-Sperre in Redis; ein
überlappender zweiter Aufruf wird mit Hinweis übersprungen (`--ohne-sperre` erzwingt):

```cron
# Erinnerungen und Pflege (Beispiel; Containername anpassen)
0 7 * * *   docker exec mandari-app python manage.py send_session_reminders   >> /var/log/mandari-reminders.log 2>&1
30 7 * * *  docker exec mandari-app python manage.py send_question_reminders  >> /var/log/mandari-question-reminders.log 2>&1
15 7 * * *  docker exec mandari-app python manage.py send_task_due_reminders  >> /var/log/mandari-task-reminders.log 2>&1
0 3 * * 1   docker exec mandari-app python manage.py fetch_person_photos      >> /var/log/mandari-person-photos.log 2>&1
# Amtliche Umringe von Bebauungsplänen abrufen und Vorlagen zuordnen (Issue #598, docs/INSIGHT_GEO.md)
50 4 * * *  docker exec mandari-app python manage.py sync_plan_boundaries     >> /var/log/mandari-plan-boundaries.log 2>&1
# Verwaiste Konten (unbestätigt, abgelehnt, ohne Zuordnung) nach Frist löschen, Issue #238
45 3 * * *  docker exec mandari-app python manage.py cleanup_orphaned_accounts >> /var/log/mandari-orphaned-accounts.log 2>&1
# Betrieb (Issue #231, docs/MONITORING.md): Quellen stündlich, Service-Level täglich, Verfügbarkeitsbericht monatlich
15 * * * *  docker exec mandari-app python manage.py check_source_health    >> /var/log/mandari-source-health.log 2>&1
30 6 * * *  docker exec mandari-app python manage.py check_service_levels   >> /var/log/mandari-service-levels.log 2>&1
15 0 1 * *  docker exec mandari-app python manage.py availability_report --out /var/lib/mandari/reports/verfuegbarkeit-$(date -d "yesterday" +\%Y-\%m).md >> /var/log/mandari-availability.log 2>&1
# Protokollierung (Issue #221, docs/PROTOKOLLIERUNG.md): Hash-Ketten täglich prüfen (Exit-Code 1 bei Befund),
# Sicherheitsprotokoll nach Frist archivieren und löschen, DSGVO-Löschlauf mit Archivpaket monatlich
20 4 * * *  docker exec mandari-app python manage.py verify_audit_chain       >> /var/log/mandari-audit-chain.log 2>&1
40 4 * * *  docker exec mandari-app python manage.py purge_security_audit_log >> /var/log/mandari-security-audit.log 2>&1
0 5 1 * *   docker exec mandari-app python manage.py session_privacy_purge    >> /var/log/mandari-privacy-purge.log 2>&1
```

Nach dem Update mit der Hash-Kette (Issue #221) einmal den Altbestand verketten; bis dahin
schreiben betroffene Mandanten unverkettet weiter. Der Befehl ist wiederholbar und arbeitet in
kurzen Transaktionen:

```bash
docker exec mandari-app python manage.py audit_chain_backfill
```

Nach dem Update mit der öffentlichen Niederschrift (Issue #318) einmal die öffentliche Fassung für
bereits veröffentlichte Niederschriften erzeugen (OParl `resultsProtocol`, Bürgerportal). Der Befehl
ist wiederholbar, erzeugt nur Fehlendes und kennt `--dry-run` und `--tenant <slug>`:

```bash
docker exec mandari-app python manage.py session_publish_protocols
```

Archivpakete vor der fristgerechten Löschung landen in `AUDIT_ARCHIVE_ROOT` (Vorgabe
`<MEDIA_ROOT>/audit_archive`, also im persistenten Medien-Volume und in der Sicherung; nie per
URL abrufbar) oder in einem Speicher aus `STORAGES`, dessen Alias `AUDIT_ARCHIVE_STORAGE` nennt.
`AUDIT_EXPORT_MAX_ROWS` (Vorgabe 100000) begrenzt Exporte aus der Oberfläche, größere Zeiträume
exportiert `export_audit_log`; `SECURITY_AUDIT_RETENTION_DAYS` (Vorgabe 365) ist die Frist des
Sicherheitsprotokolls.

Dazu minütlich die Hintergrund-Erzeugung der Sitzungsmappen (Gesamt-PDF und ZIP-Paket, Issue #218).
Die Oberfläche legt nur Anforderungen an; ohne diesen Job bleibt eine Mappe bei „wird erstellt“:

```cron
* * * * *   docker exec mandari-app python manage.py build_meeting_packages --limit 5 --max-seconds 240 >> /var/log/mandari-meeting-packages.log 2>&1
```

**Abos zu Themen und Orten im Bürgerportal** (`/insight/benachrichtigungen/`) sind standardmäßig
abgeschaltet (`INSIGHT_SUBSCRIPTIONS_ENABLED=false`): keine Links im Portal, die Abo-Seiten antworten
mit 404, `generate_alerts` und `send_digest` brechen mit Hinweis ab. Abmelden über bereits versandte
Links bleibt möglich; Beschluss-Abos sind nicht betroffen. Wer die Abos einschaltet, plant beide
Befehle selbst ein (z. B. `generate_alerts` täglich, `send_digest` wöchentlich). Ein Neuaufbau der
Abos über die Datendrehscheibe ist geplant.

Im Leerlauf schreibt der Job nichts. Er läuft im Web-Container und teilt sich dessen Speicher;
`SESSION_PACKAGE_MAX_EMBED_MB` (Vorgabe 200) und `SESSION_PACKAGE_MAX_PAGES` (Vorgabe 3000) begrenzen,
wie viele PDF-Anlagen je Mappe in das Gesamt-PDF eingebunden werden – weitere erscheinen dort als
Verweisseite und bleiben im ZIP-Paket vollständig.

`check_service_levels` braucht `INSIGHT_ALERT_EMAILS` als Empfänger und erreicht die Metriken
der laufenden Instanz über `METRICS_URL` (Vorgabe `http://127.0.0.1:8000/metrics/`, also im
Container selbst). `availability_report` braucht `GATUS_URL` (Statusseite) und ein
beschreibbares Zielverzeichnis im Container; ohne erreichbare Statusseite endet der Lauf mit
Exit-Code 1.

Vor dem ersten Scharfschalten von `cleanup_orphaned_accounts` lohnt ein Probelauf mit
`--dry-run`; die Kriterien stehen in `docs/DSGVO_LOESCHKONZEPT.md`. Die Ausgaben aller
Läufe enthalten nur Zahlen, keine personenbezogenen Daten.

## 🔌 Datenbankverbindungen: Budget

Alle Dienste teilen sich **eine** PostgreSQL-Instanz. Ist deren `max_connections`
erschöpft, bekommt *jeder* Dienst `FATAL: sorry, too many clients already` — auch
einer ganz ohne eigene Last. Genau das ist am 15.09.2026 passiert, als ein
Schwachstellen-Scanner mit 304 Anfragen pro Minute (normal: 8) die Verbindungszahl
über die damalige Obergrenze trieb.

Deshalb hat jeder Dienst eine feste Obergrenze, und die Summe bleibt unter der
Obergrenze der Datenbank.

| Dienst | Obergrenze | Woher |
|---|---|---|
| mandari (Daphne, 1 Prozess) | **10** | `DB_POOL_MAX`; höchstens `DB_POOL_MAX_WAITING` Anfragen warten, der Rest bekommt 503 |
| Ingestor | 30 | SQLAlchemy `pool_size=10` + `max_overflow=20` |
| OCR-Worker | 30 | gleiches Image wie der Ingestor |
| Website (Wagtail) | 10 | eigener Container, eigene Datenbank |
| Kundenportal | 10 | eigener Container, eigene Datenbank |
| Sicherung (`pg_dump`) | 2 | nur während des Laufs |
| Reserve für Superuser | 3 | `superuser_reserved_connections`, Postgres-Vorgabe |
| **Summe** | **95** | |
| **`max_connections`** | **200** | in `docker-compose.web01.yml` |

Reserve: rund 100 Verbindungen. Wer einen Dienst hinzufügt, trägt ihn hier ein
**und** prüft die Summe.

### Einstellungen der Anwendung

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `DB_POOL` | `true` | Pool an- oder abschalten. Bei SQLite ohne Wirkung |
| `DB_POOL_MIN` | `2` | Vorgehaltene Verbindungen, damit die erste Anfrage nicht wartet |
| `DB_POOL_MAX` | `10` | Obergrenze je Prozess |
| `DB_POOL_MAX_WAITING` | `20` | So viele Anfragen dürfen auf eine Verbindung warten; jede weitere bekommt sofort 503. `0` = unbegrenzt |
| `DB_POOL_TIMEOUT` | `5` | Sekunden warten bei erschöpftem Pool, danach 503 |
| `DB_POOL_MAX_LIFETIME` | `1800` | Verbindungen nach dieser Zeit erneuern |

Bei eingeschaltetem Pool setzt die Anwendung `CONN_MAX_AGE` selbsttätig auf `0` —
Django verlangt das, weil sonst zwei Mechanismen dieselbe Verbindung verwalten
würden.

### Wenn der Pool leerläuft (Issue #344)

**Anzeichen:** Im Protokoll der Anwendung häufen sich `PoolTimeout: couldn't get a connection`
oder Antworten mit 503, die Datenbank selbst hat aber reichlich freie Verbindungen (Abfrage
unten). Seiten, die ganz aus dem Cache kommen, funktionieren weiter — das täuscht.

**Ursache, die am 22.09.2026 zugeschlagen hat:** Unter ASGI bekommt jede Anfrage ihren
*eigenen* Thread; eine Anfragespitze stellt also beliebig viele Threads vor die zehn
Verbindungen. Legt ein Client auf, bricht asgiref die Aufgabe ab, und Django verschickt bei
einem Teil dieser Anfragen nie `request_finished` — die Verbindung blieb dann für immer
ausgeliehen. Ein Schwachstellen-Scanner, der Dutzende Anfragen pro Sekunde schickt und sofort
auflegt, räumte so den Pool binnen Sekunden leer, bis zum Neustart gut 25 Stunden später.

**Was heute dagegen schützt:**

| Schutz | Wo |
|---|---|
| Verbindung wird im Thread der View zurückgegeben, auch nach einem Abbruch | `ReleaseDatabaseConnectionsMiddleware`, ganz vorn in `MIDDLEWARE` |
| Fehlerseiten geben ihre Verbindung zurück (Django rendert sie unter ASGI in Executor-Threads, die nie eine Anfrage abschließen) | Dekorator an `handler_400/403/404/500` in `mandari/urls.py` |
| Eigene Threads geben ihre Verbindung zurück | Dekorator `releases_db_connections` (Readiness-Prüfung, Admin-Sync), `close_thread_connections()` nach jedem Lauf des Sync-Watchdogs |
| Eine Welle prallt schnell ab, statt Threads zu stapeln | `DB_POOL_MAX_WAITING`, `DB_POOL_TIMEOUT` |
| Leerer Pool liefert 503 mit `Retry-After`, ohne selbst die Datenbank zu brauchen | `DatabaseErrorMiddleware`, `handler_500` |
| Festgefahrener Pool wird erkannt | `/health/live/` antwortet 503, wenn eine Minute lang keine Verbindung zurückkam, die Datenbank aber erreichbar ist |
| Offensichtliche Scanner-Pfade erreichen die Anwendung gar nicht | Block `@scanner` im `Caddyfile` |

Wer eigene Hintergrund-Threads schreibt, die die Datenbank benutzen, versieht die
Thread-Funktion mit `@releases_db_connections` (aus `apps.common.db_connections`). Ein
Thread, der seine Verbindung nicht selbst schließt, nimmt sie mit ins Grab.

Den Zustand des Pools zeigt der Metriken-Endpunkt (`mandari_db_pool_connections`).

### Automatischer Neustart

Kubernetes startet einen Pod mit roter Liveness von selbst neu. **Docker Compose tut das
nicht** — ein ungesunder Container läuft einfach weiter. Dafür liegt
`deploy/scripts/restart-unhealthy.sh` bei: Es startet jeden Container mit dem Label
`mandari.autoheal=true` neu, den Docker als `unhealthy` meldet, höchstens einmal je fünf
Minuten, und schreibt jeden Neustart ins Systemprotokoll (Kennung `mandari-autoheal`).
Einrichtung, z. B. per Cron:

```
* * * * * root sh /opt/mandari/deploy/scripts/restart-unhealthy.sh >> /var/log/mandari-autoheal.log 2>&1
```

Ein Neustart ist die Notbremse, keine Lösung. Taucht `mandari-autoheal` im Protokoll auf,
lohnt der Blick, *warum* der Pool festgefahren war.

### Prüfen, was tatsächlich offen ist

```bash
docker exec staging-postgres psql -U mandari -d postgres -c "
select datname, count(*) as verbindungen, count(*) filter (where state='idle') as idle
from pg_stat_activity where backend_type='client backend' group by 1 order by 2 desc;"
```

Gemessener Normalbetrieb (16.09.2026): mandari 13, Portal 3, Website 2.

### Wenn der Ingestor der Engpass wird

Der Ingestor darf mit 30 Verbindungen mehr als die Anwendung. Das ist historisch
und nicht gemessen — wer hier Luft braucht, kürzt zuerst dort
(`ingestor/src/storage/database.py`, `pool_size` und `max_overflow`).

## 🧾 Protokolle

Container-Logs laufen auf Produktionshosts über `journald` (90 Tage, höchstens 2 GB,
überstehen die Neuerstellung von Containern), Zugriffslogs von Caddy 14 Tage, das
fachliche Audit-Log je Mandant in der Datenbank. Einrichtung in drei Schritten:

```bash
sudo sh deploy/logging/install.sh mandari admin@example.org      # journald-Drop-in, logrotate, Überlaufwarnung
docker compose -f docker-compose.yml -f deploy/logging/docker-compose.journald.yml up -d
# Caddy: roll_keep_for 336h in den Zugriffslog-Blöcken, dann caddy reload
```

Fristen, Begründungen, Sicherung und Zugriffsschutz: [docs/PROTOKOLLE.md](docs/PROTOKOLLE.md).

## 🚨 Troubleshooting

Container heißen nach `COMPOSE_PROJECT_NAME` (Vorgabe `mandari`, Dienst `mandari`).

### Deployment schlägt fehl

```bash
# 1. Logs prüfen
docker compose logs --tail=100 mandari

# 2. Container-Status
docker compose ps

# 3. Health-Check manuell
curl -s https://<domain>/health/ready/
```

### Container startet nicht

```bash
# Logs anschauen
docker logs mandari

# Container neu starten
docker compose restart mandari

# Alles neu starten
docker compose down && docker compose up -d
```

---

## 📊 Monitoring

### Basis-Monitoring

```bash
docker compose ps                # Status aller Dienste
docker compose logs -f mandari   # Live-Logs der Anwendung
```

### Health-Endpoints

| Endpoint | Beschreibung |
|----------|--------------|
| `/health/` | Datenbankprüfung für bestehende Healthchecks (Compose, Statusseite) |
| `/health/live/` | Liveness: Prozess antwortet, Datenbank-Pool nicht festgefahren |
| `/health/ready/` | Readiness: Datenbank, Cache, Elasticsearch und Medienspeicher |

### Metriken (optional)

Für erweiteres Monitoring empfohlen:
- **Hetzner Cloud Console** - CPU, RAM, Netzwerk
- **Sentry** - Error Tracking
- **Prometheus + Grafana** - Metriken

Metriken-Endpunkt, Alarme und Statusseite: [docs/MONITORING.md](docs/MONITORING.md).

---

## 🔒 Sicherheit

- TLS: Caddy holt und erneuert die Zertifikate selbst
- Alle Secrets in der `.env` (nie im Code!); `install.sh` erzeugt sie
- Daten verschlüsselt (AES-256-GCM)
- Härtung des Hosts (SSH nur mit Schlüssel, Firewall, fail2ban) richtet der Installer nicht ein;
  sie liegt beim Betreiber

### Ursprünge, Hosts und Cookies

Die Anwendung vertraut für Formulare (CSRF) und WebSockets nur dem eigenen Host und ausdrücklich
genannten Ursprüngen – nie pauschal allen Subdomains, denn dort können andere Anwendungen oder
Inhalte liegen (z. B. eine Demo-Instanz).

| Variable | Pflicht | Wirkung |
|----------|---------|---------|
| `SITE_URL` | ja | Öffentliche Adresse mit Schema (`https://mandari.example.com`). Gilt immer als vertrauenswürdiger Ursprung; mit `https://` und `DEBUG=false` tragen Sitzungs- und CSRF-Cookie das Präfix `__Host-`. |
| `ALLOWED_HOSTS` | ja | Hosts, die die Anwendung beantwortet. Der Host aus `SITE_URL` und dessen Subdomains (`.mandari.example.com`) kommen automatisch hinzu – nötig für die Weiterleitung von Organisations-Subdomains. Daraus folgt **kein** Vertrauen für Formulare oder WebSockets. |
| `CSRF_TRUSTED_ORIGINS` | nein | Weitere vertrauenswürdige Ursprünge, kommagetrennt mit Schema, **ohne Platzhalter** (`*`). Nur nötig, wenn Formulare von einem anderen Host an die Anwendung gesendet werden oder ein vorgeschalteter Dienst den `Origin` umschreibt. Organisations-Subdomains und `PORTAL_HOSTS` brauchen keinen Eintrag, weil Anfragen an den eigenen Host immer zulässig sind. |

Sitzungs- und CSRF-Cookie gelten nur für genau den Host, der sie gesetzt hat (kein `Domain`-Attribut).
Mit HTTPS heißen sie `__Host-sessionid` und `__Host-csrftoken`; Browser nehmen solche Cookies nur ohne
`Domain`, mit `Secure` und `Path=/` an, sodass keine Subdomain sie setzen oder überschreiben kann. Die
Umstellung auf diese Namen beendet beim ersten Deployment einmalig alle bestehenden Sitzungen; Werkzeuge,
die das Cookie beim Namen lesen (Lasttests, Überwachung mit Anmeldung), müssen den neuen Namen verwenden.
