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

Zeitpläne und Aufbewahrung sind über die `.env` des Stacks einstellbar.

### Was gesichert wird

- **Alle PostgreSQL-Datenbanken** einzeln als `pg_dump`-Archiv (Custom-Format),
  dazu Rollen und Rechte (`pg_dumpall --globals-only`). Die Dumps entstehen mit
  derselben PostgreSQL-Hauptversion wie die Datenbank.
- **Dateien:** Dokument-Cache (`mandari_files`) und Uploads (`mandari_media`).
- **Konfiguration:** Installationsverzeichnis inklusive `.env` (enthält den
  `ENCRYPTION_MASTER_KEY` – ohne ihn sind verschlüsselte Felder nach einer
  Wiederherstellung unlesbar) sowie die TLS-Daten von Caddy.
- Weitere Verzeichnisse lassen sich per `docker-compose.override.yml` unter
  `/source/…` einhängen.

Nicht gesichert werden Elasticsearch (Suchindex, lässt sich aus der Datenbank
neu aufbauen) und Redis (Cache, Sitzungen).

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
Problem besteht, geht höchstens alle sechs Stunden eine Mail raus.

### Statusdateien (`status/`)

| Datei | Bedeutung |
|---|---|
| `last_success` | Zeitpunkt der letzten vollständig erfolgreichen Sicherung |
| `last_error` | Fehler der letzten Sicherung (wird bei Erfolg entfernt) |
| `repo-<label>.last_success` | letzte erfolgreiche Sicherung je Repository |
| `check_ok` / `check_failed` | Ergebnis der letzten Integritätsprüfung |
| `restore_test_ok` / `restore_test_failed` | Ergebnis des letzten Wiederherstellungstests |

## Bedienung

```bash
docker exec mandari-backup mandari-backup snapshots          # Stände und Größe je Repository
docker exec mandari-backup mandari-backup run                # Sicherung sofort ausführen
docker exec mandari-backup mandari-backup check              # Integritätsprüfung
docker exec mandari-backup mandari-backup restore-test 2     # Test gegen Repository 2
docker exec mandari-backup mandari-backup restic 1 ls latest /work/dumps   # restic direkt
docker logs --tail 200 mandari-backup
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
6. mandari-Stack starten; Suchindex neu aufbauen (Elasticsearch wird nicht gesichert).
7. Backup-Stack starten und eine Sicherung auslösen.

## Grenzen

- Dateien werden ohne Dateisystem-Snapshot gelesen; Änderungen während der
  Sicherung landen im nächsten Stand. Die Datenbank ist über `pg_dump`
  transaktionskonsistent.
- Die Aufbewahrung zählt ab dem neuesten Stand: Fällt die Sicherung länger
  aus, bleiben ältere Stände erhalten, bis wieder gesichert wird.
- Wer Schreibzugriff auf den Server hat, kann mit dem hinterlegten Schlüssel
  auch Sicherungen löschen. Schutz davor bieten Ziele mit unveränderlichen
  Snapshots oder ein zusätzlich gepflegtes, nur anhängendes Repository.
