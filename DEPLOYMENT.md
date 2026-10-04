# Mandari 2.0 - Deployment Guide

## Übersicht

| Weg | Wann nutzen |
|-----|-------------|
| `install.sh` | Erstinstallation auf einem Server mit Docker Compose (siehe [README](README.md#installation)) |
| `update.sh` | Aktualisieren für Selbstbetreiber: Sicherung, Migration, Umschalten, Prüfung, Rückfall |
| `deploy/scripts/deploy.sh` | Nicht interaktiv (Betrieb, Cron, CI), gleiche Prüfung und gleicher Rückfall |
| `deploy/scripts/staging_update.sh` | Staging als Prüfstand: zieht per systemd-Timer den neuesten grünen `dev`-Stand nach ([unten](#staging-als-prüfstand)) |
| `install-k8s.sh` bzw. Helm | Kubernetes, siehe [`deploy/kubernetes/README.md`](deploy/kubernetes/README.md) |

Die Images baut `.github/workflows/release.yml` und legt sie in der GitHub Container Registry ab
(`ghcr.io/mandarioss/mandari`, `ghcr.io/mandarioss/ingestor`): `dev` → `:dev` und `:dev-<commit>`,
`main` → `:latest` und `:main-<commit>`, ein Release → `:<version>` (z. B. `:v0.11.0`) und `:latest`.
Die Website (`ghcr.io/mandarioss/website`) entsteht im Repository
[mandariOSS/marketing-website](https://github.com/mandariOSS/marketing-website); derselbe Workflow gibt ihr
jeweils dieselben Tags (Kopie des aktuellen Website-Stands), denn `docker-compose.yml` und das Helm-Chart
ziehen alle drei Images mit **einem** `IMAGE_TAG`. Danach prüft er ohne Anmeldung, dass jedes Tag für alle
drei Images abrufbar ist (`scripts/check_image_tags.py`, auch von Hand nutzbar:
`python scripts/check_image_tags.py v0.11.0 latest dev`).
Commit-Tags im Paket `website` haben zwei Quellen: `dev-<commit>`/`main-<commit>` aus diesem Workflow nennen einen
mandari-Commit, die gleichnamigen Tags aus dem Website-Repository einen Website-Commit. Welcher Website-Stand in
einem Image steckt, zeigt das Label `org.opencontainers.image.revision`. Beweglich sind nur `latest` und `dev`;
ein anderes Tag, das es schon mit anderem Inhalt gibt, überschreibt der Workflow nicht (Warnung im Lauf).

Ein Push deployt nichts; umgeschaltet wird auf dem Server mit `update.sh` oder `deploy/scripts/deploy.sh`.
Einzige Ausnahme ist eine Staging-Umgebung mit `deploy/scripts/staging_update.sh`: Sie holt sich neue
`dev`-Stände selbst, sobald CI und Release-Lauf grün sind ([Staging als Prüfstand](#staging-als-prüfstand)).
`install.sh --tag …` und `update.sh --tag …` prüfen vorab, ob es das Tag für alle drei Images gibt, und
brechen sonst mit einer Meldung ab, bevor sie etwas verändern.

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
./update.sh --tag v0.11.0  # bestimmte Version (https://github.com/mandariOSS/mandari/releases)
./update.sh --tag dev      # Entwicklungsstand (instabil)
./update.sh --dry-run      # nur prüfen, nichts ändern
./update.sh --rollback     # auf die vorherige Version zurück
```

### Mit Skript: Gesundheitsprüfung und automatischer Rückfall

`deploy/scripts/deploy.sh` ist nicht interaktiv (für Betrieb, Cron, CI) und macht aus
jedem Deploy einen geprüften Vorgang: Sicherung, `safemigrate`, Umschalten mit `--wait`,
dann **Anwendungsprüfung im Container** (`deploy/scripts/verify_deploy.py`: Readiness,
Anmeldeseite, Bürgerportal, OParl-System, optional angemeldete Demo-Seiten, jeweils mit
Inhaltsprüfung) und **Worker-Prüfung**: Beendet sich ein Container aus `WORKER_SERVICES` in den
ersten `WORKER_CHECK_SECONDS` (Standard 60) mit einem Exit-Code ungleich 0 (etwa in einer
Neustart-Schleife nach einem Startfehler), gilt der Deploy als gescheitert; Exit 0 ist planmäßig, weil die Worker nach jedem
Durchlauf enden und neu starten. Worker mit Healthcheck (`worker`, `worker-heavy`: Heartbeat-Datei
von `events_worker`) müssen außerdem binnen `WORKER_HEALTH_SECONDS` (Standard 120) „healthy“ werden,
und die Anwendungsprüfung verlangt, dass lebende Worker alle Rollen bedienen, die die Installation
braucht (`VERIFY_WORKER_SECONDS`, Standard 90; Issue #574). Ein Stand ohne laufenden Worker fällt so
beim Deploy auf, nicht erst, wenn Erinnerungen ausbleiben. Scheitert eine der Prüfungen, schaltet das Skript **selbsttätig auf
das vorherige Image zurück** und meldet das per Mail. Jeder Lauf schreibt eine Zeile in `deploy-log.tsv`
(alt, neu, Ergebnis, Unterbrechung in Sekunden, Dauer), die Grundlage für die Kennzahl
„Ausfallzeit je Deploy“ aus dem Verfügbarkeitskonzept.

```bash
# Umgebung einmalig in einer Datei ablegen (Dienstnamen, Compose-Dateien, Empfänger)
set -a; . /opt/mandari/deploy.env; set +a
sh deploy/scripts/deploy.sh plan   v0.11.0   # Images ziehen, migrate --plan, check
sh deploy/scripts/deploy.sh apply  v0.11.0   # Sicherung, Migration, Umschalten, Prüfung, ggf. Rückfall
sh deploy/scripts/deploy.sh verify           # nur die Prüfungen (Seiten + Worker) gegen den laufenden Stand
sh deploy/scripts/deploy.sh rollback <tag>   # von Hand zurück auf ein früheres Tag
```

Alle Parameter (`MANDARI_DIR`, `COMPOSE_FILES`, `APP_SERVICE`, `WORKER_SERVICES`,
`WORKER_CHECK_SECONDS`, `DB_SERVICE`, `BACKUP_DIR`, `NOTIFY_EMAIL`, `VERIFY_*`) stehen im Kopf
des Skripts.
Migrationen müssen abwärtskompatibel sein: Der Rückfall rollt Code zurück, keine
Migrationen (`django-safemigrate` spielt nur verträgliche Migrationen vor dem Umschalten ein).
Das interaktive `update.sh` für Selbst-Hoster nutzt dieselbe Anwendungsprüfung und rollt
bei Fehlschlag ebenfalls zurück. Wie `deploy.sh` hält es die Worker (`WORKER_SERVICES`,
Standard `ingestor minutes-orchestrator worker worker-heavy`) während aller Migrationen an und startet sie erst danach
mit dem neuen Image – auch bei Abbruch oder Rückfall werden sie wieder gestartet. Die Worker für
Ereignisse und Aufträge (`worker`, `worker-heavy`) starten schon nach den Migrationen vor dem
Umschalten der Anwendung (**Reihenfolge Migration → Worker → Web**), damit Aufträge der neuen
Webprozesse sofort abgearbeitet werden. Der
Protokoll-Orchestrator (`minutes-orchestrator`) nutzt das Anwendungs-Image und wechselt nach den
Migrationen immer mit, auch wenn ein eigenes `WORKER_SERVICES` ihn nicht nennt; `--rollback`
setzt ihn ebenfalls zurück. Dienste, die die Compose-Datei nicht kennt, überspringt das Skript.

Für `deploy.sh` gehört der Orchestrator ebenfalls in `WORKER_SERVICES`
(z. B. `WORKER_SERVICES="worker worker-heavy ingestor minutes-orchestrator"`); sonst läuft er nach dem
Deploy mit dem alten Image weiter. Ohne Angabe nimmt `deploy.sh` die Dienste `worker` und
`worker-heavy` (soweit die Compose-Datei sie kennt) und den Ingestor. Es startet alle `WORKER_SERVICES` nach der Migration **vor** der Anwendung
(Migration → Worker → Web) und prüft sie danach wie bisher.

### Staging als Prüfstand

Eine Staging-Umgebung kann sich selbst auf dem neuesten geprüften `dev`-Stand halten.
`deploy/scripts/staging_update.sh` läuft dafür alle fünf Minuten per systemd-Timer und arbeitet
**pull-basiert**: Der Server fragt die öffentliche GitHub-API, GitHub braucht keinen Zugang zum Server.

Ein Lauf

1. ermittelt das neueste Commit auf `dev`, für das der **Release-Lauf** (Images gebaut und veröffentlicht)
   **und die CI** erfolgreich waren. Als CI zählt der Lauf nach dem Push auf `dev` oder der Lauf in der
   Merge-Queue für genau dieses Commit;
2. vergleicht dessen Tag `dev-<commit>` mit dem laufenden `IMAGE_TAG`. Gleich oder älter: nichts zu tun;
3. ruft sonst den vorhandenen Deploy-Weg auf: `deploy.sh plan <tag>` (Images ziehen, `migrate --plan`,
   `check`), dann `deploy.sh apply <tag>` mit Sicherung, Migration, Umschalten, Anwendungs- und
   Worker-Prüfung und automatischem Rückfall;
4. legt die gesamte Ausgabe als **Prüfprotokoll** ab (`<STATE_DIR>/protokolle/<zeit>-<tag>.log`, dazu die
   Zeile in `deploy-log.tsv` und der Status in `<STATE_DIR>/status`).

Damit zeigt Staging einen neuen Stand spätestens rund 30 Minuten nach grüner CI (fünf Minuten bis zur
nächsten Abfrage plus Deploy). Ein Tag, dessen `apply` gescheitert ist, versucht das Skript nicht noch
einmal; eine gescheiterte Vorbereitung (`plan`, etwa ein nicht abrufbares Image) höchstens dreimal. Der
nächste neuere Stand wird wieder versucht. Ein Commit, das nur Markdown ändert, startet auf `dev` keine CI
und wird deshalb nicht einzeln ausgerollt; es kommt mit dem nächsten Commit mit.

**Schutz vor Verwechslung:** Das Skript arbeitet nur in einem Verzeichnis, dessen `.env` die Zeile
`MANDARI_UMGEBUNG=staging` enthält, und bricht sonst ab, bevor es etwas verändert. Eine Produktion trägt
diese Zeile nie.

**Einrichtung** (als root auf dem Staging-Server; Voraussetzungen `curl` und `jq`):

1. Staging-Installation wie gewohnt in einem eigenen Verzeichnis, mit eigener Datenbank und eigener `.env`.
   In deren `.env` die Zeile `MANDARI_UMGEBUNG=staging` ergänzen.
2. Die Umgebung für `deploy.sh` (Dienstnamen, Compose-Dateien, `BACKUP_DIR`, `NOTIFY_EMAIL`, `VERIFY_*`)
   in `<staging>/deploy.env` ablegen, wie oben unter [Mit Skript](#mit-skript-gesundheitsprüfung-und-automatischer-rückfall).
3. `deploy.sh`, `verify_deploy.py` und `staging_update.sh` aus `deploy/scripts/` gemeinsam in ein
   Verzeichnis kopieren, z. B. `<staging>/deploy/`.
4. `deploy/systemd/staging-update.env.example` als `/etc/mandari/staging-update.env` (Rechte 600)
   ablegen und `MANDARI_DIR`, `UPDATE_SKRIPT` und `DEPLOY_ENV` eintragen.
5. Probelauf ohne Änderung: `sudo sh -c 'set -a; . /etc/mandari/staging-update.env; set +a; sh "$UPDATE_SKRIPT" pruefen'`
   zeigt, welchen Stand ein Lauf ausrollen würde.
6. `deploy/systemd/mandari-staging-update.service` und `.timer` nach `/etc/systemd/system/` kopieren,
   dann `systemctl daemon-reload && systemctl enable --now mandari-staging-update.timer`.

**Im Betrieb:**

```bash
systemctl list-timers mandari-staging-update.timer      # nächster Lauf
journalctl -u mandari-staging-update.service -n 50      # letzte Läufe
systemctl start mandari-staging-update.service          # sofort prüfen und ggf. ausrollen
touch <STATE_DIR>/pause                                 # pausieren (z. B. für einen Test auf festem Stand)
rm <STATE_DIR>/pause                                    # fortsetzen
```

`<STATE_DIR>` ist `<staging>/staging-update`, wenn nicht anders gesetzt. Dort liegen auch das Log
(`staging-update.log`), die Liste gescheiterter Tags (`fehlgeschlagen`; eine Zeile entfernen erlaubt einen
neuen Versuch) und die Sperre gegen Doppelläufe. Ohne Token stellt ein Lauf drei Anfragen an die
GitHub-API (bei neuem Stand vier); das Limit von 60 Anfragen je Stunde reicht damit aus. Für mehr Spielraum
kann `GITHUB_TOKEN` ein Token ohne jede Berechtigung enthalten.

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
- [ ] Worker läuft (`docker compose ps worker` „healthy“), `python manage.py events_scheduler --list`
  zeigt die Zeitpläne (Abschnitt „Geplante Aufgaben“); in der Crontab nur Aufgaben des Betriebssystems

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
Kurzfassung (Rechenweg und Annahmen dort, Abschnitt 6):

| Klasse | Gleichzeitige Nutzer | vCPU | RAM | Anwendungsprozesse | PostgreSQL `mem_limit` |
|---|---:|---:|---:|---:|---:|
| klein (rund 20.000 Einwohner) | 20 | 2 | 8 GB | 1 | 1 GB |
| mittel (rund 100.000 Einwohner) | 100 | 4 | 16 GB | 2 | 4 GB |
| groß (500.000+ Einwohner, Bezirksvertretungen) | 400 | 12 | 32 GB | 8 | 8 GB |

Die mitgelieferte `docker-compose.yml` startet genau einen Anwendungsprozess; er trägt
rund 15 Anfragen je Sekunde (gemessen mit dem Bestand „klein“; beim Bestand „groß“ kostet
jede Anfrage doppelt so viel Rechenzeit, ein Prozess trägt dort also etwa die Hälfte, siehe
docs/LASTTESTS.md, Abschnitt 5.3). Mehrere Prozesse für „mittel“ und „groß“: Issue #718.

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

## ⚙️ Worker: Ereignisse, Aufträge und Zeitpläne

`manage.py events_worker` führt Sequenzierer, Zustellung, Aufträge und Zeitpläne in einem Prozess
aus (Grundlagen: `docs/adr/20260929-ereignistechnik-postgres.md`,
`docs/adr/20260929-auftraege-und-zeitplaene.md`). Jede Rolle läuft in einem eigenen Faden;
Leader-Rollen (Sequenzierer, Zeitpläne, je Abonnement die Zustellung) halten eine Lease in
`events_lease`, die nach 30 s ohne Erneuerung abläuft. Mehrere Worker teilen sich so die Arbeit
ohne Doppelläufe.

```bash
python manage.py events_worker                                    # alle Rollen, alle Warteschlangen
python manage.py events_worker --queues default,mail,index,ai,adapter
python manage.py events_worker --roles tasks --queues ocr,ai --max-memory-mb 800   # zweiter Worker
python manage.py events_worker --heartbeat-file /tmp/mandari-worker.heartbeat
```

| Option | Bedeutung |
|---|---|
| `--roles` | `sequencer`, `dispatch`, `tasks`, `scheduler` (Standard: alle; ohne PostgreSQL entfällt `sequencer`). Hätte eine ausdrücklich gewählte Rolle nichts zu tun (`dispatch` ohne passendes Abonnement, `tasks` ohne Warteschlange mit Parallelität), bricht der Start mit Fehler ab |
| `--queues` | Warteschlangen für Aufträge und Abonnements (Standard: alle) |
| `--subscription`, `--concurrency`, `--max-tasks`, `--max-memory-mb` | wie bei `events_dispatch` und `events_tasks` |
| `--heartbeat-file` | wird alle 5 s erneuert, solange **jede** Rolle arbeitet; Healthcheck: Änderungszeit jünger als 60 s |
| `--metrics-port`, `--metrics-addr` | `/metrics` und `/health` des Workers, Standard `0.0.0.0:9091`; `0` schaltet ab |
| `--stale-after` | ohne Lebenszeichen so lange gilt eine Rolle als hängend (Standard 300 s) |
| `--shutdown-timeout` | beim Beenden so lange auf laufende Aufträge warten (Standard 20 s) |
| `--no-restart` | statt eines Neustarts per `exec` mit Exit-Code 75 enden |

- **Beenden (SIGTERM/SIGINT):** Sequenzierer und Zustellung schreiben ihren laufenden Batch fest,
  die Zustellung samt Cursor; laufende Aufträge dürfen bis `--shutdown-timeout` zu Ende laufen,
  danach werden sie freigegeben und von einem anderen Runner erneut ausgeführt; alle Leases werden
  freigegeben. Ein zweites Signal gibt laufende Aufträge sofort frei. Danach haben alle Rollen
  zusammen noch 5 s, um zu enden; der Worker endet also spätestens nach `--shutdown-timeout` + 5 s,
  auch wenn mehrere Rollen hängen. Die Frist bis SIGKILL (`stop_grace_period` bzw.
  `terminationGracePeriodSeconds`) sollte darüber liegen, etwa 30 s.
- **Neustart:** Nach `TASKS_MAX_TASKS_PER_PROCESS` Aufträgen, oberhalb von `TASKS_MAX_MEMORY_MB`
  oder nach einer Zeitüberschreitung nimmt der Runner nichts Neues mehr an und wartet auf seine
  laufenden Aufträge; die anderen Rollen arbeiten währenddessen weiter. Danach ersetzt sich der
  Prozess per `exec` (gleiche Prozessnummer, kein Container-Neustart).
- **Ausfall:** Endet eine Rolle unerwartet, beendet sich der Worker mit Exit-Code 1; die
  Neustartregel des Containers startet ihn neu.
- **Lebenszeichen:** Hängt eine Rolle länger als `--stale-after`, erneuert der Worker weder die
  Heartbeat-Datei noch seinen Eintrag in `events_worker`; `/health` antwortet dann 503.
- **Speicher:** `TASKS_MAX_MEMORY_MB` (Standard 400) unter das Speicherlimit des Containers legen.
  Texterkennung und KI (`ocr`, `ai`) laufen in einem eigenen Worker (Dienst `worker-heavy`): Jeder
  Neustart des Runners wartet auf den längsten laufenden Auftrag seines Prozesses (`ocr` bis
  30 min); im selben Prozess stünden Mails und Suche so lange.

Die Einzelbefehle `events_sequencer`, `events_dispatch`, `events_tasks` und `events_scheduler`
bleiben für Fehlersuche und Handbetrieb (`--once`, `--list`, geparkte Ereignisse).

### Betrieb als Dienste `worker` und `worker-heavy`

Jede Installationsart bringt zwei Worker als eigene Dienste aus dem Anwendungs-Image mit, mit
derselben Umgebung wie die Anwendung:

| Dienst | Rollen und Warteschlangen | Speicherlimit |
|---|---|---|
| `worker` | alle Rollen; Aufträge und Abonnements aus `default`, `mail`, `index`, `adapter` | 1 GB (Runner-Neustart ab 400 MB; der Rest für die Verwaltungsbefehle der Zeitpläne, die als eigene Prozesse laufen) |
| `worker-heavy` | nur `tasks`; Aufträge aus `ocr` und `ai` (Texterkennung, KI) | 1 GB (Runner-Neustart ab 800 MB) |

Getrennt sind sie, weil jeder Neustart eines Runners (Zahl der Aufträge, Speichergrenze) auf seinen
längsten laufenden Auftrag wartet, bei `ocr` bis zu 30 Minuten; Mails und Suchindex warten so nie
auf die Texterkennung. `worker-heavy` ist nicht der OCR-Worker aus dem Ingestor-Image.

| Installation | Wo | Lebenszeichen |
|---|---|---|
| Ein Server (Compose) | Dienste `worker` und `worker-heavy` in `docker-compose.yml` | Healthcheck über die Heartbeat-Datei (jünger als 60 s); `restart-unhealthy.sh` startet sie neu (Label `mandari.autoheal`) |
| Mehrere Server | Rolle `worker` (`deploy/roles/worker.yml`), `worker` mit `EVENTS_DB_DIRECT_URL` direkt zu PostgreSQL | wie oben |
| Kubernetes (Helm) | `templates/worker.yaml`, Werte unter `worker.*` und `worker.heavy.*` (`worker.heavy.enabled: false`: ein Worker für alles, so in `values-minimal.yaml`) | Start- und Liveness-Probe auf `/health` (Port 9091) |

`install.sh` startet beide nach den Migrationen; `update.sh` und `deploy/scripts/deploy.sh` halten
sie während der Migrationen an und starten sie danach vor der Anwendung (Migration → Worker →
Web). Mit Helm läuft der Migrations-Job vor jedem Upgrade; Worker und Anwendung rollen danach
gemeinsam aus. Mit Helm und `persistence.accessMode: ReadWriteOnce` müssen Anwendung, Ingestor und
Worker auf einem Knoten laufen (`worker.affinity`); mehrere Knoten brauchen `ReadWriteMany`.

**Zeitpläne im Worker** (Issue #515): Wiederkehrende Arbeit läuft als Zeitplan (`schedules.py`
der Apps), nicht mehr in einem Faden im Webprozess. Der Scheduler legt je Termin genau einen
Auftrag an, auch mit mehreren Workern; ein verpasster Termin wird einmal nachgeholt.

| Zeitplan | Wann | Was |
|---|---|---|
| `insight_sync.schedules.haengende_syncs_bereinigen` | alle 5 min | Sync-Protokolle, die länger als 15 min laufen, als fehlgeschlagen markieren |
| `insight_core.schedules.verortung_automatisch` | alle `GEOREF_AUTO_INTERVAL_MINUTES` (15) min | begrenzter Verortungslauf (`GEOREF_AUTO_ENABLED`, `GEOREF_AUTO_LIMIT`) |
| `apps.work.schedules.fraktionserinnerungen_senden` | alle `FACTION_REMINDER_INTERVAL_MINUTES` (15) min | Erinnerungen an Fraktionssitzungen |
| `apps.work.schedules.fraktionseinladungen_senden` | alle `FACTION_INVITATION_INTERVAL_MINUTES` (15) min | automatische Einladungen und Freigabe-Hinweise |
| `apps.work.schedules.fraktionssitzungen_erzeugen` | alle `FACTION_SCHEDULE_INTERVAL_MINUTES` (60) min | Sitzungen aus Sitzungsreihen |
| `apps.events.schedules.idempotenzschluessel_aufraeumen` | täglich 03:40 | Idempotenzschlüssel nach `EVENTS_IDEMPOTENCY_RETENTION_DAYS` |
| `apps.events.schedules.auftraege_aufraeumen` | täglich 03:50 | beendete Aufträge: erledigte nach 14, tote und fehlgeschlagene nach 90 Tagen |

`python manage.py events_scheduler --list` zeigt alle Zeitpläne mit dem zuletzt geplanten Termin. Auch Sync
einer Quelle und Löschen einer Kommune aus dem Admin laufen als Auftrag im Worker (Warteschlange
`default`), unabhängig von `TASKS_BACKEND`.

**Wann meldet die Anwendung ein Fehlen?** Wenn die Installation Worker braucht, und das ist wegen
der Zeitpläne der Standard: Dann melden `/health/` und `/health/ready/` `"degraded"` (Antwort
bleibt 200, die Anwendung bleibt in Betrieb), und Admin-Startseite und Betriebsmonitor zeigen den
Hinweis „Worker“. Gebraucht werden:

| Einstellung | Nötige Rollen |
|---|---|
| Standard (`EVENTS_WORKER_REQUIRED` leer) | `scheduler` und `tasks` für die Warteschlangen der Zeitpläne und der Admin-Aufträge (`default`) |
| `TASKS_BACKEND=journal` | zusätzlich `tasks` für **jede** Warteschlange (zusammen über alle Worker; ausgenommen Parallelität 0) |
| `INGESTOR_EVENTS_ENABLED=true` | zusätzlich `sequencer` |
| `SESSION_EVENTS=schatten` oder `aktiv` | zusätzlich `sequencer` |
| `EVENTS_WORKER_REQUIRED=true` | alle Rollen, mit `tasks` wie oben |
| `EVENTS_WORKER_REQUIRED=false` | keine (Meldung aus; ohne Worker laufen dann auch keine Zeitpläne, etwa auf einer Vorführinstanz) |

Fällt etwa nur `worker-heavy` aus, lautet der Hinweis „kein Worker für tasks (Warteschlangen ai,
ocr)“. Die Zustellung an Abonnements (`dispatch`) prüft die Meldung nur mit
`EVENTS_WORKER_REQUIRED=true`; ihren Rückstand zeigt `mandari_events_lag_seconds`
(`docs/MONITORING.md`). Als laufend gilt ein Worker, dessen Rollen alle arbeiten und der sich in
der letzten Minute in `events_worker` gemeldet hat.

**Umschalten der Aufträge** auf die Worker: erst prüfen, dass beide laufen
(`docker compose ps worker worker-heavy`, Admin-Hinweis), dann `TASKS_BACKEND=journal` in der
`.env` setzen und Anwendung und Worker neu starten.

### Suchindex als Abonnement (Schattenbetrieb)

Heute schreiben Django-Signale, der Ingestor und `reindex_elasticsearch` den Suchindex. Künftig
schreibt ihn nur das Abonnement `suchindex` aus den `ris.*`-Ereignissen der Datendrehscheibe
(Issue #526, Umschalten in #527). Vor dem Umschalten läuft es im Schattenbetrieb: Es pflegt
Schattenindizes `schatten-papers`, `schatten-meetings`, `schatten-persons`,
`schatten-organizations` und `schatten-files` mit derselben Abbildung wie der Live-Index, der
unverändert weiterläuft.

| Einstellung | Bedeutung |
|---|---|
| `SEARCH_INDEX_SUBSCRIPTION` | `aus` (Standard), `schatten`, `aktiv` (erst mit #527) |
| `SEARCH_INDEX_SHADOW_BODIES` | Kommunen des Schattenbetriebs (Kennungen, kommagetrennt; leer = alle) |
| `SEARCH_INDEX_SHADOW_MAX_DOCS` | ungefähre Obergrenze aller Schattendokumente (Standard 100000, 0 = keine) |

- Jedes Ereignis baut die betroffenen Dokumente aus dem **aktuellen** Bestand (Dokumentbauer wie
  `reindex_elasticsearch`) und schreibt sie mit externer Version gleich der Folgenummer: Ein älterer
  Stand verliert, wiederholte oder nachgespielte Ereignisse schaden nicht.
- Abhängige Dokumente: Eine Datei oder Beratung aktualisiert auch ihren Vorgang; ein Vorgang bzw.
  eine Sitzung aktualisiert die indexierbaren Dateien, die direkt an ihm bzw. ihr hängen.
- Berücksichtigt werden öffentliche Ereignisse und die Texterkennung (`ris.file.text_extracted`,
  laut Vertrag `intern`): Die Dokumente entstehen in beiden Fällen nur aus dem RIS-Bestand.
- Ist Elasticsearch nicht erreichbar, wartet die Zustellung und stellt denselben Batch erneut zu;
  lehnt es ein Dokument ab, wird nur dessen Ereignis geparkt (Admin „Abonnements“).
- Ist die Obergrenze erreicht, werden vorhandene Dokumente weiter aktualisiert, aber keine neuen
  angelegt (Protokoll und `mandari_search_subscription_documents_total{result="skipped_limit"}`).
  Der Vollbau prüft vorab, wie viele Dokumente **danach** im Schattenindex liegen (auch die der
  schon gebauten Kommunen), und bricht sonst ab.
- Nachspielen, erneut Zustellen, Verwerfen und `loeschen` stehen wie die Eingriffe im Admin im
  Sicherheitsprotokoll (Ereignis „Eingriff in den Betrieb“, Quelle `kommandozeile`).

**Erwartete, erklärbare Abweichungen im Vergleich** (bis Issue #821 erledigt ist):

- Dateien, deren Text nach dem Vollbau erkannt wurde: Die Texterkennung meldet noch kein Ereignis
  (`ris.file.text_extracted` hat einen Vertrag, aber keinen Erzeuger). Sie fehlen im Schattenindex,
  ebenso ihr Text in der Vorschau des Vorgangs.
- Gremien und Personen: Für sie gibt es nur Löschmeldungen; Änderungen erreichen den Schattenindex
  erst über einen neuen Vollbau.
- Dateien an Vorgängen: `meeting_name` und `meeting_date` kommen aus der Sitzung der Beratung; eine
  geänderte Sitzung aktualisiert nur die Dateien, die direkt an ihr hängen. `agenda_number` folgt dem
  Tagesordnungspunkt, dessen Ereignisse (`ris.agendaitem.*`) das Abonnement nicht bekommt.
- `organization_names` von Sitzungen, Vorgängen und Dateien: Ein umbenanntes Gremium ändert sie erst
  mit dem nächsten Ereignis des jeweiligen Objekts.
- Felder, die der Ingestor im Live-Index mit eigenem Dokumentbauer anders schreibt.

Alles andere ist ein Befund vor dem Umschalten (#527).

```bash
python manage.py suchindex_schatten status                   # Schalter, Cursor, Rückstand, Größe, Heap
python manage.py suchindex_schatten aufbauen --trocken       # zählen, was der Vollbau schreiben würde
python manage.py suchindex_schatten aufbauen                 # Vollbau der gewählten Kommunen
python manage.py suchindex_schatten vergleichen [--json]     # Bestand, live, Schatten je Index und Kommune
python manage.py events_dispatch --replay suchindex --from-seq 1   # ältere Ereignisse nachholen
python manage.py suchindex_schatten loeschen --ja [--abonnement]   # Rückfall: Schattenindizes entfernen
```

**Einschalten (Compose):** erst eine Kommune, dann messen, dann erweitern.

1. Vorher messen: `docker compose exec mandari python manage.py suchindex_schatten status`
   (Heap von Elasticsearch) und `docker stats --no-stream` (Speicher von `elasticsearch` und `worker`).
2. In der `.env` `SEARCH_INDEX_SUBSCRIPTION=schatten` und `SEARCH_INDEX_SHADOW_BODIES=<Kennung>`
   setzen, dann `docker compose up -d worker mandari` (Worker und Anwendung lesen die Einstellung).
3. `docker compose run --rm --no-deps mandari python manage.py suchindex_schatten aufbauen --trocken`,
   dann ohne `--trocken` (eigener Container, nicht im Worker).
4. Nach einem Tag `suchindex_schatten vergleichen` und erneut messen wie in Schritt 1. Fehlende
   Dokumente nennen Kennungen; abweichende Felder zeigen, welcher Weg welches Feld anders schreibt.
5. Erweitern, indem weitere Kennungen in `SEARCH_INDEX_SHADOW_BODIES` kommen, Worker neu starten und
   `aufbauen --kommune <neue Kennung> --trocken`, dann ohne `--trocken` ausführen. Die Ausgabe nennt,
   wie viele Dokumente danach im Schattenindex liegen; über der Obergrenze bricht der Vollbau ab.

Idempotenzprobe nach dem Vollbau (optional): `events_dispatch --replay suchindex --from-seq 1`. Weil
der Vollbau neuer ist, bleiben `indexed` und `deleted` der Kennzahl bei 0 (außer für Dateien, deren
Text erst nach dem Vollbau erkannt wurde); es wachsen nur `stale`, `skipped_body` und `absent`
(Löschen eines Objekts, das nicht im Index steht, etwa eine Datei ohne erkannten Text).

**Umschalten (erst mit #527):** `SEARCH_INDEX_SUBSCRIPTION=aktiv` allein genügt nicht. Steht das
Abonnement in der Datenbank auf `schatten`, schreibt es weiter nur den Schattenindex. Im Admin
(„Ereignistechnik → Abonnements“) erst „Pausieren“, dann „Fortsetzen (aktiv)“.

**Rückfall:** `SEARCH_INDEX_SUBSCRIPTION=aus`, `docker compose up -d worker mandari`, dann
`python manage.py suchindex_schatten loeschen --ja --abonnement`. Der Live-Index ist nie betroffen.
`loeschen` verweigert sich, solange das Abonnement noch in den Schattenindex schreibt (Schalter oder
Zustand `schatten`); `--abonnement` zusätzlich, solange es überhaupt zugestellt wird.

### Texterkennung: OCR-Worker des Ingestors oder Aufträge `file.extract_text`

Den Text der RIS-Dateien erkennt eine Implementierung, die Bibliothek `mandari_dokumente` in `shared/`
(`docs/adr/20261004-texterkennung-shared.md`): pypdf, optional Mistral, sonst Tesseract. Tesseract läuft
Seite für Seite als eigener Unterprozess mit Grenzen (Issue #817); eine zu große Seite beendet nur diese
Seite, nicht den Prozess. Wer sie ausführt, wählt `TEXT_EXTRACTION_RUNNER` – **in Anwendung und Ingestor
gleich setzen**, sonst arbeiten beide oder keiner:

- `ingestor` (Standard): der OCR-Worker des Ingestors (`python -m src.main extract-daemon`, Ingestor-Image).
- `worker`: Aufträge `file.extract_text` in der Warteschlange `ocr` (Dienst `worker-heavy` bzw. jeder Worker,
  der `ocr` bedient). Der Zeitplan `texterkennung_einplanen` reiht alle zwei Minuten höchstens
  `TEXT_EXTRACTION_QUEUE_DEPTH` (Standard 20) Aufträge ein; der OCR-Worker des Ingestors ruht dann. Die
  Speichergrenze des Containers (1 GB) muss `OCR_MEMORY_LIMIT_MB` und den Worker selbst tragen.

Grenzen und Regeln (gleiche Variablen in Anwendung und Ingestor):

| Variable | Standard | Wirkung |
|---|---|---|
| `OCR_MAX_MEGAPIXELS` | `8` | Bildpunkte je Seite (Mio.); große Seiten (Pläne) werden mit kleinerer Auflösung gerendert, A4 bleibt bei `OCR_DPI` |
| `OCR_DPI` | `200` | Grundauflösung |
| `OCR_MEMORY_LIMIT_MB` | `1024` | Adressraum je Unterprozess (`pdftoppm`, `tesseract`); darüber ein zweiter Versuch mit halber Auflösung, danach wird die Seite übersprungen. Unter dem Speicherlimit des Containers halten; im Compose-Dienst `ingestor` (512 MB, erkennt den Text im Sync selbst) ist `384` vorgegeben |
| `OCR_PAGE_TIMEOUT` | `120` | Sekunden je Seite und Schritt |
| `OCR_FILE_BUDGET_SECONDS` | `1200` | Zeitbudget je Datei; danach gilt der bis dahin erkannte Text |
| `OCR_MAX_PAGES` | `100` | höchstens so viele Seiten je Datei |
| `TEXT_EXTRACTION_STALE_MINUTES` | `60` | Dateien, die länger in `processing` stehen, gelten als abgebrochen (Worker beendet) und werden zurückgestellt; auch in der Anwendung setzen (Prüfung `texterkennung`) |
| `TEXT_EXTRACTION_MAX_ATTEMPTS` | `3` | nach so vielen Abbrüchen wird die Datei `failed` mit dem Grund „Speichergrenze“ statt erneut zu laufen |
| `TEXT_EXTRACTION_MAX_SIZE_MB` | `50` | größere Dateien werden übersprungen |
| `MISTRAL_API_KEY`, `MISTRAL_OCR_MODEL`, `MISTRAL_OCR_RATE_LIMIT` | leer, `pixtral-12b-2409`, `60` | Mistral vor Tesseract, Anfragen je Minute und Prozess |

Beansprucht wird in kleinen Portionen direkt vor der Bearbeitung (höchstens zwei Dateien je Platz von
`TEXT_EXTRACTION_CONCURRENCY`); `TEXT_EXTRACTION_BATCH_SIZE` begrenzt nur die Dateien je Kommune und Runde.
Eine beanspruchte Datei wartet so nie hinter einem ganzen Stapel und gilt nicht als abgebrochen, solange der
Worker lebt. Im OCR-Worker (`extract-daemon`), der die Kommunen nacheinander bearbeitet, laufen Dateien mit
einem Abbruch danach einzeln und zuletzt. Erkennt der Sync- oder Scraper-Lauf den Text selbst
(`TEXT_EXTRACTION_ENABLED`), gilt das nur je Kommune, weil dort mehrere Kommunen parallel laufen; hängende
Dateien löst auch er höchstens einmal je Minute und Lauf auf. Hängende und aufgegebene Dateien meldet die
Prüfung `texterkennung` in `/health/worker/` (`docs/MONITORING.md`) und der Betriebsmonitor unter
„Handlungsbedarf“. Wird eine aufgegebene Datei wieder auf `pending` gesetzt, bekommt sie genau einen
weiteren Versuch; gelingt er, beginnt der Zähler von vorn.

**Umstellen auf Aufträge:** Ein Worker bedient `ocr` (`docker compose ps worker-heavy`), dann
`TEXT_EXTRACTION_RUNNER=worker` in der `.env` setzen und Anwendung, Worker und Ingestor-Dienste neu starten.
Dateien, die der OCR-Worker gerade bearbeitet, löst die Zeitgrenze auf. **Rückweg:** Variable entfernen
(bzw. `ingestor`) und dieselben Dienste neu starten; eingereihte Aufträge erledigen sich noch oder finden
ihre Datei bereits bearbeitet.

## ⏰ Geplante Aufgaben (Zeitpläne im Worker)

Wiederkehrende Verwaltungsbefehle laufen als **Zeitpläne im Worker** (Issue #516,
`apps/common/schedules.py`), nicht mehr per Host-Cron. Sie sind damit in jeder Installationsart
gleich (Compose, mehrere Server, Helm), versioniert und überwacht: Ein gescheiterter Lauf erscheint
in `mandari_tasks_dead` und im Admin unter „Aufträge“. Jeder Lauf startet den Befehl wie früher
der Cron-Eintrag als eigenen Prozess (im Worker-Container), mit derselben Singleton-Sperre in
Redis und einer festen Zeitgrenze; ein Fehlschlag wird nicht wiederholt, der nächste Termin ist
die Wiederholung. Zeiten in `TIME_ZONE`, ein verpasster Termin wird einmal nachgeholt. Die
Ausgabe steht im Protokoll des Workers (`docker compose logs worker`).

| Zeitplan | Wann | Befehl |
|---|---|---|
| `befehl:send_session_reminders` | täglich 07:00 | Fristen-Erinnerungen des Sitzungsdienstes |
| `befehl:send_task_due_reminders` | täglich 07:15 | fällige Aufgaben |
| `befehl:send_question_reminders` | täglich 07:30 | Ratsfragen (ohne Wirkung, solange pausiert) |
| `befehl:fetch_person_photos` | montags 03:00 | Personenfotos |
| `befehl:sync_plan_boundaries` | täglich 04:50 | Umringe von Bebauungsplänen (Issue #598, `docs/INSIGHT_GEO.md`) |
| `befehl:cleanup_orphaned_accounts` | täglich 03:45 | verwaiste Konten nach Frist löschen (Issue #238) |
| `befehl:cache_files` | stündlich :40 | Dokument-Cache: `--limit 400`, neueste fehlende Dateien zuerst (`docs/FILE_CACHE.md`) |
| `befehl:loeschabgleich` | stündlich :15 | Löschabgleich der Dokumente mit den Quellen (Issue #787, `docs/FILE_CACHE.md`; vor dem ersten Lauf `loeschabgleich --robots` ansehen) |
| `befehl:dokumentablage` | stündlich :50 | Dokumentablage: `--aufraeumen`, mit Objektspeicher `--hochladen --aufraeumen` (Issue #788) |
| `befehl:generate_alerts` | täglich 07:45 | Benachrichtigungen der Abos zu Themen und Orten; nur mit `INSIGHT_SUBSCRIPTIONS_ENABLED` |
| `befehl:send_digest` | montags 08:00 | Wochenmail der Abos; nur mit `INSIGHT_SUBSCRIPTIONS_ENABLED` |
| `befehl:check_source_health` | stündlich :15 | Zustand der Quellen (Issue #231, `docs/MONITORING.md`) |
| `befehl:check_service_levels` | täglich 06:30 | Service-Level |
| `befehl:availability_report` | am 1. um 00:15 | Verfügbarkeitsbericht des Vormonats nach `REPORTS_ROOT`; nur mit `GATUS_URL` |
| `befehl:verify_audit_chain` | täglich 04:20 | Hash-Ketten prüfen (Issue #221; ein Befund lässt den Lauf scheitern) |
| `befehl:purge_security_audit_log` | täglich 04:40 | Sicherheitsprotokoll nach Frist |
| `befehl:session_privacy_purge` | am 1. um 05:00 | DSGVO-Löschlauf mit Archivpaket |
| `befehl:build_meeting_packages` | jede Minute | Sitzungsmappen (Issue #218): `--limit 5 --max-seconds 240` |

Dazu die Zeitpläne aus dem Abschnitt „Worker“ (Fraktionssitzungen, Verortung, Aufräumen).
`python manage.py events_scheduler --list` zeigt alle mit dem zuletzt geplanten Termin; von Hand
läuft ein Befehl weiter mit `docker compose exec worker python manage.py <befehl> --trotz-zeitplan`.
Aufrufe, die nur lesen oder berichten, laufen immer: `--dry-run`, `check_source_health --report`,
`check_service_levels --report`, `cache_files --stats`, `loeschabgleich --robots`, `dokumentablage`
ohne Schritt (Kennzahlen) und `availability_report` ohne `--out`.

**Auf dem Host** bleiben nur Aufgaben des Betriebssystems: die Datensicherung (`./backup.sh`,
Abschnitt „Backup“), der Journal-Alarm (`deploy/logging/journal-alert.sh`) und der Neustart
ungesunder Container (`deploy/scripts/restart-unhealthy.sh`, Abschnitt „Automatischer Neustart“).

`REPORTS_ROOT` (Vorgabe `<MEDIA_ROOT>/berichte`) liegt im Medien-Volume und damit in der Sicherung
und ist nie über `/media/` abrufbar. `check_service_levels` braucht `INSIGHT_ALERT_EMAILS` als
Empfänger und erreicht die Metriken der Anwendung über `METRICS_URL` (Compose-Vorgabe
`http://mandari:8000/metrics/`, der Dienstname der Anwendung; auf einem getrennten Worker-Server die
Adresse des Web-Servers im privaten Netz). Diese Einstellungen braucht jetzt der **Worker**; in
eigenen Compose-Dateien also auch in seiner `environment`.

### Umstellung von Host-Cron (Upgrade-Hinweis)

Bestehende Installationen hatten die Befehle oben in der Crontab des Hosts (auch `cache_files`,
`loeschabgleich` und `dokumentablage --aufraeumen` aus `docs/FILE_CACHE.md` und, bei eingeschalteten
Abos, `generate_alerts`/`send_digest`). Die Umstellung
läuft ohne Doppelläufe und ohne Lücke:

1. **Worker zuerst:** `docker compose ps worker` zeigt `healthy`, `/health/` meldet
   `"worker": "ok"`. Ohne laufenden Worker gibt es keine Zeitpläne. Im Rollenbetrieb
   (`docs/MEHR_SERVER_BETRIEB.md`) vorher die gemeinsame Ablage für Medien und Dokument-Cache auf
   dem worker-Server einrichten – Sitzungsmappen, Personenfotos und `cache_files` laufen dort.
2. **Update einspielen.** Ab jetzt planen die Zeitpläne. Ein noch vorhandener Cron-Eintrag ruft den
   Befehl weiter auf; der erkennt, dass ein Worker seinen Zeitplan bedient (Scheduler und Runner
   für `default` leben), und endet mit dem Hinweis „läuft als Zeitplan im Worker – Aufruf
   übersprungen“ (Exit-Code 0, in seiner Logdatei). Läuft kein Worker, arbeitet der Cron-Eintrag
   wie bisher. Kontrolle nach einem Tag:

   ```bash
   grep -h "Aufruf übersprungen" /var/log/mandari-*.log | tail
   docker compose logs --since 24h worker | grep "Zeitplan befehl:"
   ```

3. **Crontab bereinigen**, wenn die Zeitpläne laufen. Erst sichern, dann die Zeilen der Befehle oben
   entfernen. Übrig bleiben nur Aufgaben des Betriebssystems (Datensicherung, Journal-Alarm, Neustart
   ungesunder Container); steht danach noch ein `manage.py`-Aufruf in der Crontab, gehört er als
   Zeitplan in den Code (`apps/common/schedules.py`):

   ```bash
   crontab -l > ~/crontab-vor-zeitplaenen-$(date +%Y%m%d).txt
   crontab -l | grep -v -E 'manage\.py (send_session_reminders|send_task_due_reminders|send_question_reminders|fetch_person_photos|sync_plan_boundaries|cleanup_orphaned_accounts|cache_files|loeschabgleich|dokumentablage|generate_alerts|send_digest|check_source_health|check_service_levels|availability_report|verify_audit_chain|purge_security_audit_log|session_privacy_purge|build_meeting_packages)' | crontab -
   crontab -l | grep 'manage\.py' || echo "keine Verwaltungsbefehle mehr in der Crontab"
   ```

4. **Rückweg:** einzelne Zeitpläne abschalten mit `EVENTS_SCHEDULES_DISABLED=befehl:<name>`
   (kommagetrennt) in der `.env` und `docker compose up -d worker mandari`; ihre Termine
   verstreichen dann ohne Auftrag, und ein noch vorhandener (oder aus der Sicherung
   zurückgespielter) Cron-Eintrag läuft wieder. Beim Wiedereinschalten wird nichts nachgeholt.
   Insgesamt zurück geht es mit dem vorherigen Image (`deploy.sh rollback <tag>`) und der gesicherten
   Crontab. Beim späteren erneuten Update holt jeder Zeitplan seinen letzten verpassten Termin einmal
   nach, auch wenn ihn in der Zwischenzeit der Cron-Eintrag erledigt hat. Die Befehle sind weitgehend
   wiederholbar (Erinnerungen und Benachrichtigungen je Objekt und Frist einmal); ein Service-Level-
   oder Quellenalarm kann dabei ein zweites Mal kommen.

Nach dem Update mit der Hash-Kette (Issue #221) einmal den Altbestand verketten; bis dahin
schreiben betroffene Mandanten unverkettet weiter. Der Befehl ist wiederholbar und arbeitet in
kurzen Transaktionen:

```bash
docker compose exec mandari python manage.py audit_chain_backfill
```

Nach dem Update mit der öffentlichen Niederschrift (Issue #318) einmal die öffentliche Fassung für
bereits veröffentlichte Niederschriften erzeugen (OParl `resultsProtocol`, Bürgerportal). Der Befehl
ist wiederholbar, erzeugt nur Fehlendes und kennt `--dry-run` und `--tenant <slug>`:

```bash
docker compose exec mandari python manage.py session_publish_protocols
```

Archivpakete vor der fristgerechten Löschung landen in `AUDIT_ARCHIVE_ROOT` (Vorgabe
`<MEDIA_ROOT>/audit_archive`, also im persistenten Medien-Volume und in der Sicherung; nie per
URL abrufbar) oder in einem Speicher aus `STORAGES`, dessen Alias `AUDIT_ARCHIVE_STORAGE` nennt.
`AUDIT_EXPORT_MAX_ROWS` (Vorgabe 100000) begrenzt Exporte aus der Oberfläche, größere Zeiträume
exportiert `export_audit_log`; `SECURITY_AUDIT_RETENTION_DAYS` (Vorgabe 365) ist die Frist des
Sicherheitsprotokolls.

Die Sitzungsmappen (Gesamt-PDF und ZIP-Paket, Issue #218) erzeugt der Zeitplan
`befehl:build_meeting_packages`; die Oberfläche legt nur Anforderungen an, ohne Worker bleibt eine
Mappe bei „wird erstellt“. Im Leerlauf schreibt der Lauf nichts. Er läuft als eigener Prozess im Worker-Container (1 GB);
`SESSION_PACKAGE_MAX_EMBED_MB` (Vorgabe 200) und `SESSION_PACKAGE_MAX_PAGES` (Vorgabe 3000) begrenzen,
wie viele PDF-Anlagen je Mappe in das Gesamt-PDF eingebunden werden – weitere erscheinen dort als
Verweisseite und bleiben im ZIP-Paket vollständig.

**Abos zu Themen und Orten im Bürgerportal** (`/insight/benachrichtigungen/`) sind standardmäßig
abgeschaltet (`INSIGHT_SUBSCRIPTIONS_ENABLED=false`): keine Links im Portal, die Abo-Seiten antworten
mit 404, `generate_alerts` und `send_digest` brechen mit Hinweis ab. Abmelden über bereits versandte
Links bleibt möglich; Beschluss-Abos sind nicht betroffen. Eingeschaltet laufen beide Befehle als
Zeitpläne im Worker (`generate_alerts` täglich 07:45, `send_digest` montags 08:00, Abschnitt
„Geplante Aufgaben“). Ein Neuaufbau der Abos über die Datendrehscheibe ist geplant.

**Ratsfragen im Bürgerportal** (`/insight/fragen/`) sind standardmäßig pausiert
(`INSIGHT_QUESTIONS_ENABLED=false`, docs/INSIGHT_QUESTIONS.md): Die bisherigen Fragen und Antworten bleiben
unter ihren Adressen lesbar, mit Hinweis und ohne Antwortquoten je Person und Fraktion. Stellen, Bestätigen
und Antworten antworten mit 404, es gehen keine Mails hinaus; `send_question_reminders` endet mit Hinweis
und ohne Wirkung, der Zeitplan kann bleiben.

`availability_report` braucht `GATUS_URL` (Statusseite); ohne erreichbare Statusseite endet der
Lauf mit Exit-Code 1 (im Zeitplan: gescheiterter Auftrag).

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
| Worker (Dienst `worker`, alle Rollen ohne `ocr`/`ai`) | 19 | Pool 18 ohne Abonnements, dazu die Direktverbindung des Weckrufs; Rechnung unten |
| Worker für Texterkennung und KI (Dienst `worker-heavy`) | 7 | nur Rolle `tasks` mit `ocr` und `ai`; Rechnung unten |
| Verwaltungsbefehle der Zeitpläne (eigene Prozesse im Dienst `worker`) | 8 | höchstens 4 gleichzeitig (Parallelität von `default`), je Prozess 1–2 Verbindungen; früher als Host-Cron im Anwendungscontainer |
| OCR-Worker | 30 | gleiches Image wie der Ingestor |
| Website (Wagtail) | 10 | eigener Container, eigene Datenbank |
| Kundenportal | 10 | eigener Container, eigene Datenbank |
| Sicherung (`pg_dump`) | 2 | nur während des Laufs |
| Reserve für Superuser | 3 | `superuser_reserved_connections`, Postgres-Vorgabe |
| **Summe** | **129** | |
| **`max_connections`** | **200** | `POSTGRES_MAX_CONNECTIONS` im eigenen Betrieb (Vorgabe der `docker-compose.yml`: 100) |

Reserve: rund 70 Verbindungen. Wer einen Dienst hinzufügt, trägt ihn hier ein
**und** prüft die Summe.

### Worker

Der Worker (`manage.py events_worker`) braucht so viele Verbindungen, wie seine Fäden gleichzeitig
arbeiten können. Er vergrößert seinen Pool beim Start selbst auf diese Zahl (ein höheres
`DB_POOL_MAX` bleibt) und nennt sie in seiner Startzeile („bis zu … Datenbankverbindungen“):

| Teil | Verbindungen |
|---|---|
| Hauptfaden (Lebenszeichen in `events_worker`) und Reserve | 1 + 2 |
| Rolle `sequencer` | 1 |
| Rolle `dispatch` | 1 je Abonnement |
| Rolle `tasks` | 1 (Koordinator) + Summe der Parallelität der gewählten Warteschlangen (Standard: `default` 4, `mail` 2, `index` 2, `ocr` 1, `ai` 1, `adapter` 2, zusammen 12) |
| Rolle `scheduler` | 1 |
| Selbstprüfung des Weckrufs (mit `sequencer` oder `dispatch`) | 1 |
| Abruf von `/metrics` (außer mit `--metrics-port 0`) | 1 |
| Lauschverbindung des Weckrufs, am Pool vorbei | +1 in der Datenbank |

Mit allen Rollen und allen Standard-Warteschlangen ohne Abonnements sind das 20 aus dem Pool und
21 in der Datenbank; jedes Abonnement kommt mit einer hinzu. Der Dienst `worker` ohne `ocr` und
`ai` braucht 18 aus dem Pool und 19 in der Datenbank, `worker-heavy`
(`--roles tasks --queues ocr,ai`) 7. Hinter PgBouncer zählt der Pool des
Workers gegen dessen `default_pool_size`, die Lauschverbindung geht über `EVENTS_DB_DIRECT_URL`
direkt zur Datenbank.

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
| Eigene Threads geben ihre Verbindung zurück | Dekorator `releases_db_connections` (Readiness-Prüfung); Hintergrundarbeit läuft nicht mehr in Fäden des Webprozesses, sondern als Auftrag oder Zeitplan im Worker (Issue #515) |
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
docker exec mandari-postgres psql -U mandari -d postgres -c "
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
| `/health/` | Datenbankprüfung für bestehende Healthchecks (Compose, Statusseite); Feld `worker` |
| `/health/live/` | Liveness: Prozess antwortet, Datenbank-Pool nicht festgefahren |
| `/health/ready/` | Readiness: Datenbank, Cache, Elasticsearch und Medienspeicher; `worker` nur als Hinweis (`degraded`) |

### Metriken (optional)

Für erweitertes Monitoring empfohlen:
- **Überwachung des Hosting-Anbieters** - CPU, RAM, Netzwerk
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
