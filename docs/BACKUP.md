# Backups mit restic

mandari sichert Datenbanken, Dateien und Konfiguration täglich verschlüsselt
in zwei Repositorys an getrennten Standorten. Der Backup-Stack liegt in
[`deploy/backup/`](../deploy/backup/) und läuft als eigener Container neben
dem mandari-Stack.

## Überblick

| Was | Wann (Standard) | Wie |
|---|---|---|
| Sicherung | täglich 02:30 | Datenbank-Dumps erstellen, alles in beide Repositorys sichern |
| Aufbewahrung | nach jeder Sicherung | je Tag der letzte Stand, höchstens 30 Tage (`forget --keep-within-daily 30d --prune`) |
| Integritätsprüfung | sonntags 06:30 | `restic check`, liest rotierend ein Viertel der Daten zurück (alle Daten in vier Wochen) |
| Wiederherstellungstest | am 3. jedes Monats, 04:30 | Hauptdatenbank in eine Wegwerf-Datenbank einspielen, Zeilenzahlen und Datei-Prüfsummen vergleichen |
| Überwachung | stündlich (Host) | Mail bei Fehler, veralteter Sicherung, fehlgeschlagener Prüfung oder fehlgeschlagenem Test |
| Zurück auf einen Zeitpunkt (wahlweise, `BACKUP_PITR=1`) | WAL fortlaufend, Basissicherung mit der täglichen Sicherung, WAL alle 15 Minuten außer Haus | physische Basissicherung und archiviertes Write-Ahead-Log; jeder Zeitpunkt der letzten 7 Tage ist erreichbar, siehe [Zurück auf einen Zeitpunkt](#zurück-auf-einen-zeitpunkt-point-in-time-recovery) |

Zeitpläne und Aufbewahrung sind über die `.env` des Stacks einstellbar.

### Was gesichert wird

- **Alle PostgreSQL-Datenbanken** einzeln als `pg_dump`-Archiv (Custom-Format),
  dazu Rollen und Rechte (`pg_dumpall --globals-only`). Die Dumps entstehen mit
  derselben PostgreSQL-Hauptversion wie die Datenbank.
- **Dateien:** Dokument-Cache (`mandari_files`) und Uploads (`mandari_media`).
- **Konfiguration:** Installationsverzeichnis inklusive `.env` (enthält den
  `ENCRYPTION_MASTER_KEY` – ohne ihn sind verschlüsselte Felder nach einer
  Wiederherstellung unlesbar) sowie die TLS-Daten von Caddy. Nach einem
  Schlüsselwechsel passt eine ältere Datenbanksicherung nur zum damaligen Schlüssel
  aus derselben Sicherung. Soll sie in eine Installation mit neuem Schlüssel
  zurück, wird der damalige als `ENCRYPTION_MASTER_KEY_PREVIOUS` gesetzt und
  `rotate_encryption --master-only` ausgeführt (siehe `docs/KRYPTOKONZEPT.md`,
  Abschnitt 5).
- **Protokolle:** das systemd-Journal des Hosts (`/var/log/journal`, Container- und
  Anwendungslogs, 90 Tage) und das Zugriffslog im Caddy-Volume; Fristen und
  Zugriffsschutz in `docs/PROTOKOLLIERUNG.md` (Übersicht und Abschnitt 12).
- Weitere Verzeichnisse lassen sich per `docker-compose.override.yml` unter
  `/source/…` einhängen.

Nicht gesichert werden Elasticsearch (Suchindex, lässt sich aus der Datenbank
neu aufbauen) und Redis (Cache, Sitzungen).

### Journal und Aufträge

Die Ereignistechnik (`apps/events`) hält ihren ganzen Zustand in der Hauptdatenbank: das Journal
(`events_event`) mit Folgenummern, Abonnements mit Cursor, geparkte Ereignisse, Aufträge
(`events_task`), Zeitpläne und Leases. Der Datenbank-Dump enthält sie transaktionskonsistent; eine
Datenbank-Sicht passt nach dem Einspielen zu ihrem Cursor, laufende Aufträge und Leases laufen nach
ihrer Frist ab (Auftrag 60 s, Lease 30 s) und werden ohne Handarbeit wieder aufgenommen.

Zwei Dinge liegen außerhalb der Sicherung und brauchen nach dem Einspielen einen Schritt:

1. **Folgenummer anheben.** Zwischen Sicherung und Ausfall hat der Sequenzierer weitere Nummern
   vergeben; der Suchindex (externe Version gleich Folgenummer) und Abnehmer des Änderungsfeeds
   (Cursor) kennen sie. Ohne Anheben bekämen neue Ereignisse dieselben Nummern: Der Suchindex verwürfe
   sie als veraltet, ein Abnehmer des Feeds überspränge sie. Deshalb **vor dem Start des Workers**:
   `python manage.py events_after_restore --apply`. Der Befehl hebt die Sequenz um 100 000 000 über
   das Ende des Journals; ein zweiter Aufruf ändert nichts, und solange ein Sequenzierer eine gültige
   Lease hält, verweigert er. Ohne `--apply` zeigt er nur den Stand: Journal, Abonnements (Sicht oder
   externes Ziel), Geparktes, Aufträge; ein Cursor hinter dem Ende des Journals beendet ihn mit
   Exit-Code 1. Abnehmer des Feeds, deren Cursor auf ein verlorenes Ereignis zeigt, erhalten `410` und
   steigen über den Snapshot neu ein.
2. **Externe Ziele neu aufbauen oder nachspielen.** Was ein Abonnement außerhalb der Datenbank
   schreibt (Suchindex, später Webhooks und Adapter), steht auf einem neueren Stand als das Journal.
   Der Suchindex wird neu aufgebaut (`reindex_elasticsearch --clear`, im Schattenbetrieb
   `suchindex_schatten loeschen` und `aufbauen`). Eine Datenbank-Sicht lässt sich jederzeit aus dem
   Journal nachspielen: Sicht leeren, `events_dispatch --replay <abonnement> --from-seq 1`.

`./backup.sh --restore` erledigt Schritt 1 selbst (nach dem Einspielen der Datenbank, vor dem Start
der Dienste) und baut den Suchindex neu auf. Der monatliche Wiederherstellungstest prüft zusätzlich,
dass kein Cursor hinter dem Ende des Journals steht und die Sequenz nicht darunter liegt.

### Sicherheit

- Die Daten werden **vor der Übertragung verschlüsselt** (restic: AES-256 im
  Counter-Modus, Poly1305-AES-Authentifizierung). Das Zielsystem sieht nur
  verschlüsselte Blöcke.
- Übertragung per SFTP mit eigenem SSH-Schlüssel und festgelegtem Host-Key
  (`StrictHostKeyChecking=yes`). Auf dem Zielsystem wird kein Programm
  ausgeführt; reiner SFTP-Speicher genügt.
- Quellen sind nur lesend eingehängt; der Container hat keinen Zugriff auf
  den Docker-Socket.
- Deduplizierung und Kompression: 30 Tagesstände belegen kaum mehr Platz als
  ein vollständiger Stand plus die täglichen Änderungen.

> **Das restic-Passwort ist der Schlüssel zu allen Sicherungen.** Es muss
> zusätzlich außerhalb des Servers verwahrt werden (Passwort-Manager,
> Notfallumschlag). Geht es verloren, sind die Sicherungen unbrauchbar.

## Einrichtung

Voraussetzungen: Docker Compose, laufender mandari-Stack, zwei SFTP-Ziele
(ein Ziel ist möglich, zwei sind für den Produktivbetrieb dringend empfohlen).

```bash
cd deploy/backup
cp .env.example .env
chmod 600 .env
# Werte eintragen: RESTIC_REPO_1/2, BACKUP_PG_PASSWORD, Quellpfade, BACKUP_ALERT_TO

# SSH-Schlüssel und Host-Keys
install -d -m 700 ssh secrets status work
ssh-keygen -t ed25519 -N "" -C "mandari-backup" -f ssh/id_ed25519
ssh-keyscan backup-host-1.example backup-host-2.example > ssh/known_hosts
# Fingerprints mit den Angaben des Anbieters vergleichen, dann
# ssh/id_ed25519.pub auf den Zielen in .ssh/authorized_keys eintragen
# (manche Speicherdienste verlangen das RFC4716-Format: ssh-keygen -e -f ssh/id_ed25519.pub)

# restic-Passwort erzeugen und SICHER zusätzlich außerhalb verwahren
(umask 077; openssl rand -base64 48 | tr -d '\n' > secrets/restic_password)

docker compose build
docker compose run --rm backup mandari-backup init
docker compose run --rm backup mandari-backup run      # erste Sicherung (dauert je nach Datenmenge)
docker compose up -d
```

### Überwachung auf dem Host

`scripts/backup-watchdog.sh` prüft stündlich die Statusdateien und schickt bei
Problemen eine Mail über den mandari-Container (SMTP aus den
Systemeinstellungen). Einrichtung als root:

```bash
chmod 755 scripts/backup-watchdog.sh
scripts/backup-watchdog.sh --test     # Testmail an BACKUP_ALERT_TO
( crontab -l 2>/dev/null; echo "5 * * * * $PWD/scripts/backup-watchdog.sh >> /var/log/mandari-backup-watchdog.log 2>&1" ) | crontab -
```

Gemeldet wird, wenn der Container nicht läuft, die letzte Sicherung mit Fehler
endete oder älter als `BACKUP_MAX_AGE_HOURS` (Standard 26 Stunden) ist, oder
wenn Integritätsprüfung bzw. Wiederherstellungstest fehlschlugen. Solange ein
Problem besteht, geht höchstens alle sechs Stunden eine Mail raus. Mit `BACKUP_PITR=1`
kommen die Warnungen zu WAL-Empfang, Slot und Basissicherung dazu
([Überwachung des WAL-Empfangs](#überwachung-des-wal-empfangs)).

### Statusdateien (`status/`)

| Datei | Bedeutung |
|---|---|
| `last_success` | Zeitpunkt der letzten vollständig erfolgreichen Sicherung |
| `last_error` | Fehler der letzten Sicherung (wird bei Erfolg entfernt) |
| `repo-<label>.last_success` | letzte erfolgreiche Sicherung je Repository |
| `check_ok` / `check_failed` | Ergebnis der letzten Integritätsprüfung |
| `restore_test_ok` / `restore_test_failed` | Ergebnis des letzten Wiederherstellungstests, mit `BACKUP_PITR=1` einschließlich der Zeitpunkt-Probe |
| `pitr_empfang_ok` / `pitr_empfang_failed` | nur mit `BACKUP_PITR=1`: Zustand von WAL-Empfang und Slot, geprüft mit jeder WAL-Übertragung |
| `pitr_basis_last_success` / `pitr_basis_last_error` | letzte Basissicherung in allen Repositorys bzw. ihr Fehler |
| `pitr_wal_last_success` / `pitr_wal_last_error` | letzte Übertragung des WAL-Archivs in alle Repositorys bzw. ihr Fehler |
| `pitr_kette_unterbrochen` | das WAL ließ sich nicht lückenlos fortsetzen; nennt Zeit und Grund |

## Bedienung

```bash
docker exec mandari-backup mandari-backup snapshots          # Stände und Größe je Repository
docker exec mandari-backup mandari-backup run                # Sicherung sofort ausführen
docker exec mandari-backup mandari-backup check              # Integritätsprüfung
docker exec mandari-backup mandari-backup restore-test 2     # Test gegen Repository 2
docker exec mandari-backup mandari-backup restic 1 ls latest /work/dumps   # restic direkt
docker logs --tail 200 mandari-backup

# nur mit BACKUP_PITR=1
docker exec mandari-backup mandari-backup pitr-status        # WAL-Empfang, Ketten, Basissicherungen
docker exec mandari-backup mandari-backup pitr --zeitpunkt "2026-11-10 16:54"   # Probe in einer Wegwerf-Instanz
```

Lange Läufe im Hintergrund starten, damit sie das Ende der SSH-Sitzung
überstehen: `docker exec -d mandari-backup sh -c 'mandari-backup run > /proc/1/fd/1 2>&1'`.

## Wiederherstellung

Alle Befehle laufen im Backup-Container; `N` ist die Nummer des Repositorys
(1 oder 2). Wiederhergestellt wird zunächst nach `/work/restore` und von dort
gezielt übernommen – nie direkt über laufende Daten.

### Stand auswählen

```bash
docker exec mandari-backup mandari-backup restic 1 snapshots
docker exec mandari-backup mandari-backup restic 1 ls <snapshot-id> /work/dumps
```

### Einzelne Datenbank

```bash
# Dump aus dem Stand holen
docker exec mandari-backup mandari-backup restic 1 restore <snapshot-id> \
  --target /work/restore --include /work/dumps/mandari.dump

# mandari-, Ingestor- und Worker-Container stoppen, dann einspielen
docker exec mandari-backup sh -c \
  'pg_restore -h "$BACKUP_PG_HOST" -U "$BACKUP_PG_USER" --clean --if-exists -d mandari /work/restore/work/dumps/mandari.dump'
docker exec mandari-backup rm -rf /work/restore
```

Der Dump enthält die Erweiterung `pg_stat_statements` (Issue #1024), und die darf nur ein
Superuser anlegen. Mit der Rolle aus der mitgelieferten `docker-compose.yml` ist das kein
Problem. Spielt eine Rolle ohne Superuser-Rechte ein (etwa eine verwaltete Datenbank), die
Erweiterung über eine Inhaltsliste auslassen (`pg_restore -l … | grep -v pg_stat_statements`,
dann `pg_restore -L …`); Einzelheiten in `DEPLOYMENT.md`, Abschnitt „Teuerste Abfragen“.

Danach, **bevor** der Worker wieder startet, die Folgenummer anheben (Abschnitt „Journal und
Aufträge“), dann Worker, Anwendung und Ingestor starten und den Suchindex neu aufbauen:

```bash
docker compose run --rm --no-deps mandari python manage.py events_after_restore --apply
```

### Dateien und Konfiguration

```bash
docker exec mandari-backup mandari-backup restic 1 restore <snapshot-id> \
  --target /work/restore --include /source/files/<pfad>
# anschließend vom Host aus an den Zielort kopieren: <BACKUP_WORK_DIR>/restore/source/files/…
```

Die Quellen sind im Container nur lesend eingehängt; zurückkopiert wird
deshalb bewusst vom Host aus.

### Kompletter Neuaufbau (Serververlust)

1. Neuen Server mit Docker vorbereiten, mandari-Repository auschecken.
2. Backup-Stack mit **demselben restic-Passwort**, SSH-Schlüssel (oder neu
   hinterlegtem Schlüssel) und denselben Repository-Adressen einrichten;
   `mandari-backup init` ist nicht nötig.
3. Konfiguration zurückholen: `/source/config` (enthält `.env` mit
   `ENCRYPTION_MASTER_KEY`) nach `/work/restore` wiederherstellen und in das
   Installationsverzeichnis kopieren.
4. Nur PostgreSQL starten (`docker compose up -d postgres`), Rollen aus
   `globals.sql` einspielen, danach jede Datenbank mit `pg_restore` (siehe oben).
5. Dateien (`/source/files`, `/source/media`) und Caddy-Daten zurückkopieren.
6. Folgenummer anheben, bevor der Worker startet
   (`docker compose run --rm --no-deps mandari python manage.py events_after_restore --apply`).
7. mandari-Stack starten; Suchindex neu aufbauen (Elasticsearch wird nicht gesichert).
8. Backup-Stack starten und eine Sicherung auslösen.

### Wiederherstellung mit Journal: Tests

Nachspielen einer Datenbank-Sicht nach der Wiederherstellung (Sicht leeren, ab Folgenummer 1
zustellen, gleiche Prüfsumme) und das Verhalten eines externen Ziels mit externer Version prüft
`mandari/apps/events/tests/test_wiederherstellung.py`; den ganzen Rundlauf mit `./backup.sh`
einschließlich Journal prüft `.github/workflows/backup-roundtrip.yml`.

## Zurück auf einen Zeitpunkt (Point-in-Time Recovery)

Die tägliche Sicherung kennt nur den Stand der Nacht. Mit `BACKUP_PITR=1` lässt sich die Datenbank
zusätzlich auf jeden Zeitpunkt der letzten sieben Tage zurückstellen, etwa auf die Minute vor einem
versehentlichen Löschen. Der Schalter ist standardmäßig aus; ohne ihn verhält sich der Stack wie bisher
(neu ist dann nur das leere Verzeichnis `pitr/`).

So arbeitet es:

- **WAL fortlaufend.** Der Backup-Container empfängt mit `pg_receivewal` das Write-Ahead-Log der Datenbank
  über einen Replikations-Slot und schreibt es komprimiert nach `pitr/wal/`, das angefangene Segment
  eingeschlossen (`--synchronous`). Die Datenbank bleibt das unveränderte PostgreSQL-Image, ohne
  `archive_command`. Bricht der Empfänger ab, startet er nach wenigen Sekunden neu und setzt lückenlos fort;
  der Slot lässt nur einen Empfänger zu.
- **Basissicherung nachts.** Der tägliche Lauf (`BACKUP_CRON`) erstellt nach den Dumps eine physische Sicherung
  mit `pg_basebackup` (tar, gzip, mit dem WAL ihrer Laufzeit) und überträgt sie in alle Repositorys. Die
  Dumps bleiben: Sie holen einzelne Datenbanken zurück und überstehen einen Versionssprung von PostgreSQL.
- **WAL außer Haus.** Alle 15 Minuten (`BACKUP_PITR_WAL_CRON`) schließt ein kurzer Lauf das angefangene
  Segment ab und überträgt das Archiv mit restic in dieselben Repositorys. Ein geplanter Lauf (Sicherung,
  Prüfung, Test) hat Vorrang: Solange er läuft, entfällt die Übertragung, und er wartet seinerseits, statt
  auszufallen, wenn gerade eine Übertragung läuft.
- **Ketten.** Lässt sich das WAL nicht lückenlos fortsetzen (anderer Datenbank-Cluster, Slot gelöscht oder
  von der Datenbank verworfen), beginnt der Empfänger eine neue Kette in einem eigenen Verzeichnis und
  erstellt sofort eine Basissicherung dazu. Gelöscht wird dabei nichts.

In den Repositorys liegen diese Stände unter dem Namen `<BACKUP_HOSTNAME>-pitr`. Aufbewahrung und
Wiederherstellungstest der täglichen Stände berühren sie nicht.

### Wiederherstellungsziele

| Fall | Höchstens verlorene Daten | Erreichbare Zeitpunkte |
|---|---|---|
| Fehler in den Daten, Server läuft (versehentlich gelöscht, fehlerhafter Import) | nichts vor dem gewählten Zeitpunkt | jeder Zeitpunkt der letzten 7 Tage (`BACKUP_PITR_KEEP_DAYS`), auf die Sekunde, bis zur Gegenwart |
| Datenbank zerstört, Verzeichnis `pitr/` erhalten | Sekunden: was der Empfänger bis zum Ausfall geschrieben hat | wie oben, bis zur letzten empfangenen Transaktion |
| Server oder Standort verloren | der Takt der WAL-Übertragung, Standard 15 Minuten, zuzüglich ihrer Dauer; während Sicherung, Integritätsprüfung und Wiederherstellungstest deren Laufzeit | jeder Zeitpunkt der letzten 7 Tage bis zur letzten Übertragung |
| ohne `BACKUP_PITR` | bis zu 24 Stunden | nur die nächtlichen Stände |

Die Dauer einer Wiederherstellung hängt von der Größe der Datenbank und vom WAL seit der Basissicherung ab
(bei nächtlicher Basissicherung höchstens das eines Tages). Der monatliche Test misst sie mit; sie steht in
`status/restore_test_ok`. Nicht erreichbar sind Zeitpunkte vor dem Ende der ältesten behaltenen
Basissicherung und Zeitpunkte zwischen dem Ende einer WAL-Kette und der Basissicherung der nächsten
(`mandari-backup pitr-status` zeigt beides).

### Einrichtung auf der Datenbankseite

Der Empfänger braucht dreierlei in der Datenbank. `scripts/pitr-db-einrichten.sh` richtet es im
PostgreSQL-Container ein und prüft es; der Aufruf lässt sich beliebig wiederholen.

| Was | Wie | Neustart der Datenbank |
|---|---|---|
| Rolle mit `REPLICATION` und eigenem Passwort (`BACKUP_PITR_REPL_USER`, `BACKUP_PITR_REPL_PASSWORD`) | `CREATE ROLE … LOGIN REPLICATION PASSWORD …`, als SCRAM-Kennwort | nein, gilt sofort |
| Zeile in `pg_hba.conf`: `host replication <rolle> samenet scram-sha-256` | angehängt; `samenet` sind die Netze, in denen der Datenbank-Container selbst hängt | nein, `pg_reload_conf()` |
| `max_slot_wal_keep_size` (`BACKUP_PITR_SLOT_MAX_WAL`, Standard 4 GB) | `ALTER SYSTEM SET …` | nein, `pg_reload_conf()` |

Die Zeile in `pg_hba.conf` ist nötig, weil die vorhandene Regel für alle Datenbanken keine
Replikationsverbindungen einschließt. `max_slot_wal_keep_size` schützt die Datenbank: Steht der Empfänger,
hält sie höchstens so viel WAL für ihn zurück und verwirft danach den Slot, statt ihren Datenträger zu
füllen; so viel Platz muss dort frei bleiben.

Vorausgesetzt sind die Standardwerte von PostgreSQL: `wal_level = replica`, `max_wal_senders` mindestens 3,
`max_replication_slots` mindestens 1. Die mitgelieferte `docker-compose.yml` setzt keinen davon, also ist
dort **kein Neustart nötig und keine Änderung an der Compose-Datei**. Nur wer einen der drei Werte gesenkt
hat, muss ihn anheben und die Datenbank neu starten; das Skript nennt den Wert. Verlangt wird PostgreSQL 13
oder neuer, geprüft ist PostgreSQL 16.

```bash
cd deploy/backup
# in der .env: BACKUP_PITR_REPL_PASSWORD eintragen (erzeugen: openssl rand -hex 24)
install -d -m 700 pitr
scripts/pitr-db-einrichten.sh              # Rolle, pg_hba-Zeile, Schutzgrenze; endet mit einer Prüfung
# in der .env: BACKUP_PITR=1
docker compose build && docker compose up -d
docker logs -f mandari-backup              # WAL-Empfang startet, die erste Basissicherung entsteht von selbst
docker exec mandari-backup mandari-backup pitr-status
```

Ohne Docker-Zugriff auf die Datenbank (verwaltete Datenbank, Kubernetes) die drei Schritte der Tabelle von
Hand ausführen. Das Skript liest den Namen des Containers aus `BACKUP_PG_CONTAINER`, sonst `BACKUP_PG_HOST`.
`pg_hba.conf` und die Einstellung liegen im Datenverzeichnis und überstehen ein Neuanlegen des Containers;
nach einem neuen Cluster (Versionssprung, Neuaufbau) das Skript erneut ausführen.

Die Replikationsrolle kann den gesamten Datenbestand lesen. Ihr Passwort gehört wie die übrigen Zugänge
erzeugt, nicht ausgedacht, und nur in die `.env`.

### Platzbedarf

Faustformel mit `D` = Größe der Datenbank auf der Platte, `W` = WAL je Tag, `N` = Fenster in Tagen (7):

| Wo | Bedarf |
|---|---|
| `pitr/` auf dem Server | `(N + 2) × 0,3 × W` für das WAL, dazu während der Übertragung eine Basissicherung: `0,4 × D` |
| je Repository | `(N + 2) × 0,4 × D` für die Basissicherungen und `(N + 4) × 0,3 × W` für das WAL |
| `work/` während einer Wiederherstellung | `D`, dazu das komprimierte WAL von der gewählten Basissicherung bis heute (aus dem Repository: das der ganzen Kette) |
| Datenträger der Datenbank | bis `BACKUP_PITR_SLOT_MAX_WAL`, solange der Empfänger steht |

Die Faktoren 0,4 und 0,3 sind grobe Schätzwerte für gzip und im Betrieb noch nicht nachgemessen; nach
einigen Tagen zeigen `du -sh pitr/wal` und `mandari-backup snapshots` den wirklichen Bedarf.
Beispiel: 20 GB Datenbank mit 2 GB WAL je Tag brauchen rund 13 GB unter `pitr/` und rund 80 GB je
Repository.

Den größten Teil machen die Basissicherungen aus: Eine gepackte Vollsicherung je Nacht kann restic nicht
gegen die der Vornacht deduplizieren. Mit `BACKUP_PITR_COMPRESS=0` übergibt der Lauf sie unkomprimiert;
restic komprimiert selbst und speichert von Nacht zu Nacht nur, was sich geändert hat. Dafür braucht
`pitr/` während der Übertragung `D` statt `0,4 × D`.

### Aufbewahrung

Nach jeder Basissicherung räumt der Lauf beides zusammen auf (`mandari-backup pitr-plan` zeigt es, ohne zu
löschen):

- Behalten werden alle Basissicherungen, die im Fenster fertig wurden, und als Anker die jüngste davor.
  Von ihr aus ist der Anfang des Fensters erreichbar.
- Das lokale WAL wird erst ab dem Segment gelöscht, das die älteste behaltene Basissicherung braucht, und
  zwar über alle Repositorys gerechnet. Ist ein Repository nicht lesbar oder gibt es noch keine
  Basissicherung, bleibt das WAL unangetastet.
- Der neueste WAL-Stand im Repository enthält alles noch nötige WAL. Ältere WAL-Stände bleiben zwei Tage
  als Rückhalt, falls das lokale Archiv beschädigt übertragen wurde.
- Den Platz gibt das Aufräumen der täglichen Sicherung in der folgenden Nacht frei (`forget --prune`).

### Überwachung des WAL-Empfangs

Mit jeder WAL-Übertragung prüft der Container Empfänger und Slot und schreibt das Ergebnis in die
Statusdateien. `scripts/backup-watchdog.sh` meldet mit `BACKUP_PITR=1` zusätzlich:

- **WAL-Empfang steht:** Am Slot hängt kein Empfänger, der Slot fehlt oder die Datenbank ist nicht
  erreichbar (nach vier Versuchen über anderthalb Minuten, damit ein Neustart der Datenbank nicht meldet).
- **Slot staut WAL:** mehr als `BACKUP_PITR_SLOT_WARN_MB` (Standard 512 MB) zurückgehalten, oder die
  Datenbank ist dabei, WAL des Slots zu verwerfen.
- **Basissicherung veraltet:** die jüngste ist älter als `BACKUP_PITR_BASE_MAX_AGE_HOURS` (Standard 26
  Stunden) oder die letzte endete mit einem Fehler.
- **WAL-Übertragung überfällig:** seit `BACKUP_PITR_WAL_MAX_AGE_MINUTES` (Standard 90 Minuten) keine
  erfolgreiche Übertragung, oder die letzte schlug fehl. Dauert die tägliche Sicherung länger, den Wert
  anheben: Während sie läuft, ruht die Übertragung.
- **WAL-Kette unterbrochen:** einen Tag lang, mit Zeit und Grund.

Der monatliche Wiederherstellungstest enthält die **Zeitpunkt-Probe**: Er schreibt zwei Markierungen in
die laufende Datenbank (Tabelle `mandari_backup.pitr_probe` in der Datenbank `postgres`), stellt aus dem
Repository auf einen Zeitpunkt dazwischen zurück und verlangt, dass die erste da ist und die zweite fehlt.
Das Ergebnis steht in `restore_test_ok` bzw. `restore_test_failed`. Dieselbe Probe läuft in der CI gegen
eine kleine Datenbank (`deploy/backup/tests/pitr-rundlauf.sh`).

### Runbook: Zurück auf einen Zeitpunkt

Der Befehl baut den Stand immer in einer **Wegwerf-Instanz** im Backup-Container auf. Die laufende
Datenbank berührt er nicht; sie zu ersetzen ist ein eigener, bewusster Schritt (Schritt 4).

**Zeitangabe.** `--zeitpunkt "JJJJ-MM-TT HH:MM[:SS]"` gilt in der Zeitzone des Containers (`BACKUP_TZ`,
Standard `Europe/Berlin`). Mit `Z` oder einem Abstand wie `+01:00` am Ende gilt dieser. Die Ausgabe nennt
Ortszeit und UTC. In der doppelten Stunde am Ende der Sommerzeit die Angabe mit Abstand schreiben; eine
Uhrzeit, die es nicht gibt, wird abgelehnt. Enthalten ist alles, was bis zu dieser Sekunde bestätigt war.
`--zeitpunkt ende` spielt alles ein, was archiviert ist.

1. **Zeitpunkt bestimmen und Schreibzugriffe anhalten.** Soll die Datenbank ersetzt werden, zuerst
   Anwendung, Worker und Ingestor stoppen, damit nach dem Zeitpunkt nichts mehr hinzukommt, und den
   heutigen Stand sichern:

   ```bash
   docker compose stop mandari worker worker-heavy worker-live minutes-orchestrator ingestor
   docker exec mandari-backup mandari-backup run
   ```

2. **Stand aufbauen.**

   ```bash
   docker exec mandari-backup mandari-backup pitr --zeitpunkt "2026-11-10 16:54" --behalten
   ```

   Der Befehl wählt die jüngste Basissicherung, die vor dem Zeitpunkt fertig war, holt sie aus dem
   Repository (`--repo N`, Standard 1) in ein leeres Datenverzeichnis unter `work/pitr-restore`, setzt
   `restore_command` und `recovery_target_time` und startet PostgreSQL. Das WAL kommt aus dem lokalen
   Archiv, wenn es dort liegt, sonst aus dem Repository (`--wal lokal|ablage`). Am Ende stehen die
   letzte eingespielte Transaktion und die Zeilenzahlen der größten Tabellen (`--db`, Standard
   `BACKUP_RESTORE_TEST_DB`). Rückgabewert 0: Zeitpunkt erreicht. Rückgabewert 2: Das Archiv endet vor
   dem Zeitpunkt; der Stand reicht dann nur bis zur genannten Transaktion.

3. **Prüfen.** Mit `--behalten` läuft die Instanz weiter, erreichbar nur im Container:

   ```bash
   docker exec -it mandari-backup psql -h /work/pitr-restore/run -p 55433 -U mandari mandari
   ```

4. **Übernehmen.** Entweder Einzelnes zurückholen (Zeilen oder Tabellen mit `pg_dump -t … | psql` aus
   der Wegwerf-Instanz in die laufende Datenbank) oder die Datenbank ersetzen, auf demselben Weg wie
   bei der [einzelnen Datenbank](#einzelne-datenbank):

   ```bash
   docker exec mandari-backup sh -c \
     'pg_dump -h /work/pitr-restore/run -p 55433 -U "$BACKUP_PG_USER" --format=custom --compress=0 -f /work/pitr.dump mandari'
   docker exec mandari-backup sh -c \
     'pg_restore -h "$BACKUP_PG_HOST" -U "$BACKUP_PG_USER" --clean --if-exists -d mandari /work/pitr.dump'
   ```

   Das Einspielen läuft selbst durch das WAL: Auch der Stand unmittelbar davor bleibt erreichbar.

5. **Nach dem Ersetzen**, in dieser Reihenfolge:
   - **Ereignisjournal:** bevor der Worker startet, die Folgenummer anheben
     (`docker compose run --rm --no-deps mandari python manage.py events_after_restore --apply`). Der
     Sequenzierer hat nach dem Zeitpunkt weitere Nummern vergeben, die Suchindex und Abnehmer des
     Änderungsfeeds kennen; Einzelheiten im Abschnitt [Journal und Aufträge](#journal-und-aufträge).
   - **Dienste starten**, dann den **Suchindex neu aufbauen** (`reindex_elasticsearch --clear`, im
     Schattenbetrieb `suchindex_schatten loeschen` und `aufbauen`): Er steht auf einem neueren Stand als
     die Datenbank.
   - **Dateiablage:** Uploads und Dokument-Cache stellt dieser Weg nicht zurück. Dateien, die nach dem
     Zeitpunkt hinzukamen, bleiben ohne Verweis liegen. Dateien, die nach dem Zeitpunkt gelöscht wurden
     und die die Datenbank nun wieder kennt, kommen aus der täglichen Sicherung zurück
     ([Dateien und Konfiguration](#dateien-und-konfiguration)); der Dokument-Cache ist ein
     Zwischenspeicher, Fehlendes lässt sich aus dem Ratsinformationssystem neu abrufen
     (`docs/FILE_CACHE.md`). Was am selben Tag hochgeladen und wieder gelöscht
     wurde, ist in keiner Sicherung.
   - **Schlüssel:** Wurde der `ENCRYPTION_MASTER_KEY` nach dem Zeitpunkt gewechselt, gilt der Hinweis
     unter [Was gesichert wird](#was-gesichert-wird).

6. **Aufräumen.**

   ```bash
   docker exec mandari-backup rm -f /work/pitr.dump
   docker exec mandari-backup mandari-backup pitr-aufraeumen
   ```

**Serververlust.** Auf dem neuen Server den Backup-Stack wie unter
[Kompletter Neuaufbau](#kompletter-neuaufbau-serververlust) einrichten. `mandari-backup pitr --zeitpunkt ende
--behalten` braucht `BACKUP_PITR=1` nicht und holt Basissicherung und WAL aus dem Repository. Rollen
(`pg_dumpall --globals-only`) und jede Datenbank dann aus der Wegwerf-Instanz statt aus den nächtlichen
Dumps übernehmen. Danach die Datenbank neu einrichten (`scripts/pitr-db-einrichten.sh`) und PITR
einschalten; es beginnt eine neue WAL-Kette mit eigener Basissicherung.

### Störungen

- **WAL-Empfang steht.** `docker logs mandari-backup` nennt den Grund. Anmeldung und Datenbankseite prüft
  `scripts/pitr-db-einrichten.sh --pruefen`.
- **Slot staut WAL.** Meist steht der Empfänger oder `pitr/` ist voll. Überschreitet der Rückstand
  `max_slot_wal_keep_size`, verwirft die Datenbank den Slot; der Empfänger beginnt dann von selbst eine
  neue Kette mit Basissicherung, und die Zeit dazwischen ist nicht erreichbar.
- **Neu beginnen.** `mandari-backup pitr-neue-kette` verwirft den Slot und beginnt eine neue Kette, etwa
  wenn das lokale Archiv beschädigt ist. Die alte Kette bleibt, bis die Aufbewahrung sie nicht mehr braucht.
- **Ausschalten.** `BACKUP_PITR=0`, `docker compose up -d`, und danach **den Slot löschen**, sonst hält die
  Datenbank bis zur Schutzgrenze WAL zurück (Name aus `BACKUP_PITR_SLOT`, Standard `mandari_pitr`):
  `select pg_drop_replication_slot('mandari_pitr');`

## Grenzen

- Die Wiederherstellung auf einen Zeitpunkt betrifft nur die Datenbank, als Ganzes: alle Datenbanken des
  Clusters im selben Augenblick. Dateien folgen dem täglichen Stand. Zusätzliche Tablespaces werden nicht
  unterstützt. Das Ersetzen der laufenden Datenbank ist nicht vorgefertigt, sondern das Runbook oben; der
  physische Austausch des Datenverzeichnisses ist dort nicht beschrieben.
- Dateien werden ohne Dateisystem-Snapshot gelesen; Änderungen während der
  Sicherung landen im nächsten Stand. Die Datenbank ist über `pg_dump`
  transaktionskonsistent.
- Die Aufbewahrung zählt ab dem neuesten Stand: Fällt die Sicherung länger
  aus, bleiben ältere Stände erhalten, bis wieder gesichert wird.
- Gelöschte Daten (etwa nach der Löschung einer Mitgliedschaft) verschwinden aus den
  Sicherungen mit der Zeit, spätestens nach 30 Tagen. Das gilt auch für die
  Sicherungen vor jedem Deploy (`deploy.sh`, `BACKUP_MAX_AGE_DAYS`, täglich
  `deploy.sh prune`, siehe `DEPLOYMENT.md`). Sicherungen von Hand außerhalb dieser
  Ablagen unterliegen keiner Frist und sind nach 30 Tagen zu löschen.
- Wer Schreibzugriff auf den Server hat, kann mit dem hinterlegten Schlüssel
  auch Sicherungen löschen. Schutz davor bieten Ziele mit unveränderlichen
  Snapshots oder ein zusätzlich gepflegtes, nur anhängendes Repository.
