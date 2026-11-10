#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# Rundlauf der Wiederherstellung auf einen Zeitpunkt (CI, Issue #1303)
# =============================================================================
# Baut das Backup-Image, startet eine kleine Datenbank aus dem unveränderten
# PostgreSQL-Image und prüft den Stack aus docker-compose.yml gegen zwei lokale
# restic-Repositorys:
#   - ohne BACKUP_PITR bleibt alles wie bisher (drei Zeitpläne, crond als Prozess 1)
#   - pitr-db-einrichten.sh richtet die Datenbank ohne Neustart ein, zweimal ausführbar
#   - WAL-Empfang mit Slot, Neustart nach Abbruch, kein zweiter Empfänger, sauberes Ende
#   - Basissicherung, WAL-Übertragung, Zeitpunkt-Probe mit zwei Markierungen
#     (mandari-backup pitr und im Wiederherstellungstest), aus beiden Repositorys
#   - Aufbewahrung: Nach dem Aufräumen ist von der ältesten behaltenen Basissicherung
#     an jeder Zeitpunkt erreichbar
#   - Datenbank weg: Stand bis zur letzten Transaktion aus dem angefangenen Segment
#   - Warnungen des Watchdogs
# Vorweg laufen die Einzelprüfungen aus pitr-einheiten.sh im gebauten Image.
#
# Braucht Docker mit Compose, jq und sudo (Dateien der Container gehören root).
# Angelegt und am Ende entfernt werden nur Container, Netz und Dateien mit dem
# Namen pitrci sowie deploy/backup/.env; mit vorhandener .env bricht der Lauf ab.
# =============================================================================
set -euo pipefail

STACK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
NET=pitrci
DB=pitrci-postgres
PROJECT=pitrci-backup
ROLE=mandari_pitr
T=$(mktemp -d)

step() { printf '\n=== %s\n' "$*"; }
fail() {
    printf '\nFEHLER: %s\n' "$*" >&2
    exit 1
}

dc() { docker compose --project-directory "$STACK" -f "$STACK/docker-compose.yml" -f "$T/ci.yml" "$@"; }
backup() { dc exec -T backup mandari-backup "$@"; }
db_sql() { docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U mandari -d "${2:-mandari}" -Atc "$1"; }
restored_sql() { dc exec -T backup psql -h /work/pitr-restore/run -p 55433 -U mandari -d mandari -Atc "$1"; }
watchdog() { BACKUP_DIR="$STACK" "$STACK/scripts/backup-watchdog.sh"; }
slot_active() { [ "$(db_sql "select active from pg_replication_slots where slot_name = '$ROLE'" postgres)" = t ]; }
chain() { dc exec -T backup cat /pitr/wal/aktuell; }
base_count() { backup restic "$1" snapshots --host pitrci-pitr --tag pitr-basis --json | jq length; }

wait_for() {
    local limit="$1" what="$2" waited=0
    shift 2
    until "$@" >/dev/null 2>&1; do
        waited=$((waited + 1))
        [ "$waited" -lt "$limit" ] || fail "Zeit abgelaufen ($limit s): $what"
        sleep 1
    done
}

cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "----- Protokoll des Backup-Containers"
        dc logs --no-color --tail 200 backup 2>/dev/null || true
        echo "----- Statusdateien"
        sudo sh -c "tail -n +1 '$T'/status/* 2>/dev/null" || true
    fi
    dc down --volumes --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$DB" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -f "$STACK/.env"
    sudo rm -rf "$T"
    exit "$rc"
}

[ ! -e "$STACK/.env" ] || fail "$STACK/.env ist vorhanden – dieser Lauf gehört nicht in eine eingerichtete Installation"
trap cleanup EXIT

# Zwei Markierungen mit dem Zielzeitpunkt dazwischen. Setzt ZIEL (Sekunden seit 1970),
# VORHER und NACHHER (Kennungen der Zeilen).
NUMMER=30000
markierungen() {
    NUMMER=$((NUMMER + 10))
    VORHER=$((NUMMER + 1))
    NACHHER=$((NUMMER + 2))
    db_sql "insert into rundlauf (id, marke) values ($VORHER, 'vorher')" >/dev/null
    sleep 2
    ZIEL=$(date +%s)
    sleep 2
    db_sql "insert into rundlauf (id, marke) values ($NACHHER, 'nachher')" >/dev/null
}
# Die Wegwerf-Instanz enthält die erste Markierung und die zweite nicht
erwarte_stand() {
    local found
    found=$(restored_sql "select coalesce(string_agg(marke, ',' order by id), 'keine') from rundlauf where id in ($VORHER, $NACHHER)")
    [ "$found" = vorher ] || fail "$1: Markierungen im wiederhergestellten Stand: $found (erwartet: vorher)"
    echo "  $1: erste Markierung da, zweite nicht"
    backup pitr-aufraeumen
}

# -----------------------------------------------------------------------------
step "Umgebung: Datenbank aus dem unveränderten Image, Backup-Stack mit lokalen Repositorys"
mkdir -p "$T"/src/config "$T"/src/media "$T"/src/files "$T"/src/caddy "$T"/src/journal \
    "$T"/ssh "$T"/secrets "$T"/status "$T"/work "$T"/pitr "$T"/repos
echo "Konfiguration der Probe" > "$T/src/config/probe.txt"
openssl rand -hex 24 > "$T/secrets/restic_password"
DB_PASSWORD=$(openssl rand -hex 24)
cat > "$STACK/.env" <<EOF
BACKUP_COMPOSE_PROJECT=$PROJECT
BACKUP_HOSTNAME=pitrci
RESTIC_REPO_1=/repos/eins
RESTIC_REPO_1_LABEL=eins
RESTIC_REPO_2=/repos/zwei
RESTIC_REPO_2_LABEL=zwei
BACKUP_TZ=Europe/Berlin
BACKUP_CRON="0 0 1 1 *"
BACKUP_CHECK_CRON="0 0 1 1 *"
BACKUP_RESTORE_TEST_CRON="0 0 1 1 *"
BACKUP_DB_NETWORK=$NET
BACKUP_PG_HOST=$DB
BACKUP_PG_USER=mandari
BACKUP_PG_PASSWORD=$DB_PASSWORD
BACKUP_POSTGRES_IMAGE=postgres:16-alpine
BACKUP_SRC_CONFIG=$T/src/config
BACKUP_SRC_MEDIA=$T/src/media
BACKUP_SRC_FILES=$T/src/files
BACKUP_SRC_CADDY=$T/src/caddy
BACKUP_SRC_JOURNAL=$T/src/journal
BACKUP_SSH_DIR=$T/ssh
BACKUP_PASSWORD_FILE=$T/secrets/restic_password
BACKUP_STATUS_DIR=$T/status
BACKUP_WORK_DIR=$T/work
BACKUP_PITR_DIR=$T/pitr
BACKUP_PITR=0
BACKUP_PITR_REPL_USER=$ROLE
BACKUP_PITR_REPL_PASSWORD=$(openssl rand -hex 24)
BACKUP_PITR_SLOT=$ROLE
BACKUP_PITR_SLOT_MAX_WAL=1GB
BACKUP_PITR_WAL_CRON="0 0 1 1 *"
BACKUP_PITR_CHECKPOINT=fast
BACKUP_ALERT_TO=
EOF
# Lokale Repositorys statt SFTP; kurze Wartezeit der Empfangsprüfung
cat > "$T/ci.yml" <<EOF
services:
  backup:
    environment:
      BACKUP_PITR_RECHECK_SECONDS: "2"
    volumes:
      - $T/repos:/repos
EOF

docker network create "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" -e POSTGRES_USER=mandari -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB=mandari postgres:16-alpine >/dev/null
# Über TCP: Der Hilfsserver während der Ersteinrichtung hört nur auf dem Socket
wait_for 90 "Datenbank bereit" docker exec "$DB" pg_isready -h 127.0.0.1 -U mandari -d mandari
db_sql "create table rundlauf (id int primary key, marke text not null, zeit timestamptz not null default clock_timestamp());
        insert into rundlauf (id, marke) select g, 'grundbestand' from generate_series(1, 20000) g" >/dev/null
START_DB=$(db_sql "select pg_postmaster_start_time()")

dc build --quiet

# -----------------------------------------------------------------------------
step "Einzelprüfungen im Image: Zeitangaben, Aufbewahrung, Lücken, angefangenes Segment"
docker run --rm --entrypoint bash -v "$STACK/tests:/tests:ro" "$PROJECT:local" /tests/pitr-einheiten.sh

dc run --rm -T backup mandari-backup init

# -----------------------------------------------------------------------------
step "Ohne BACKUP_PITR bleibt alles wie bisher"
dc up -d
wait_for 30 "Container läuft" dc exec -T backup true
[ "$(dc exec -T backup sh -c 'grep -c . /etc/crontabs/root')" = 3 ] || fail "ohne PITR werden drei Zeitpläne erwartet"
[ "$(dc exec -T backup cat /proc/1/comm)" = crond ] || fail "ohne PITR ist crond der Prozess 1"
if dc exec -T backup pgrep -x pg_receivewal >/dev/null; then fail "ohne PITR läuft kein WAL-Empfänger"; fi
rc=0
backup pitr-wal || rc=$?
[ "$rc" = 64 ] || fail "pitr-wal ohne PITR endet mit 64, nicht $rc"
backup run
[ "$(backup restic 1 snapshots --json | jq length)" = 1 ] || fail "ohne PITR entsteht genau ein Stand"
[ -z "$(sudo ls -A "$T/pitr")" ] || fail "ohne PITR bleibt das Verzeichnis für das WAL leer"
[ -z "$(db_sql "select slot_name from pg_replication_slots" postgres)" ] || fail "ohne PITR gibt es keinen Slot"
# Besetzte Sperre: Ein geplanter Lauf bricht wie bisher sofort ab
dc exec -T -d backup flock /run/mandari-backup.lock sleep 6
sleep 1
rc=0
backup check || rc=$?
[ "$rc" = 75 ] || fail "ohne PITR endet ein Lauf bei besetzter Sperre mit 75, nicht $rc"
sleep 6

# -----------------------------------------------------------------------------
step "Datenbank einrichten: Rolle, pg_hba-Zeile, Schutzgrenze – ohne Neustart, wiederholbar"
if BACKUP_DIR="$STACK" "$STACK/scripts/pitr-db-einrichten.sh" --pruefen; then
    fail "vor der Einrichtung darf die Prüfung nicht bestehen"
fi
BACKUP_DIR="$STACK" "$STACK/scripts/pitr-db-einrichten.sh"
second=$(BACKUP_DIR="$STACK" "$STACK/scripts/pitr-db-einrichten.sh")
echo "$second"
if grep -Eq "angelegt|ergänzt|auf .* gesetzt|neu geladen" <<< "$second"; then fail "der zweite Aufruf darf nichts mehr ändern"; fi
BACKUP_DIR="$STACK" "$STACK/scripts/pitr-db-einrichten.sh" --pruefen
# shellcheck disable=SC2016  # läuft in der Shell des Containers
[ "$(docker exec "$DB" sh -c 'grep -c "replication.*mandari_pitr" "$PGDATA/pg_hba.conf"')" = 1 ] ||
    fail "pg_hba.conf enthält die Zeile nicht genau einmal"
[ "$(db_sql "show max_slot_wal_keep_size")" = 1GB ] || fail "max_slot_wal_keep_size ist nicht gesetzt"
[ "$(db_sql "select pg_postmaster_start_time()")" = "$START_DB" ] || fail "die Datenbank wurde neu gestartet"

# -----------------------------------------------------------------------------
step "PITR einschalten: WAL-Empfang und erste Basissicherung von selbst"
sed -i 's/^BACKUP_PITR=0$/BACKUP_PITR=1/' "$STACK/.env"
dc up -d
wait_for 60 "Slot aktiv" slot_active
wait_for 180 "erste Basissicherung" test -s "$T/status/pitr_basis_last_success"
cat "$T/status/pitr_basis_last_success"
[ "$(dc exec -T backup sh -c 'grep -c . /etc/crontabs/root')" = 4 ] || fail "mit PITR werden vier Zeitpläne erwartet"
[ "$(db_sql "select pg_postmaster_start_time()")" = "$START_DB" ] || fail "die Datenbank wurde neu gestartet"
KETTE=$(chain)
rc=0
backup pitr-empfang || rc=$?
[ "$rc" = 75 ] || fail "ein zweiter Empfänger muss abgelehnt werden (75), nicht $rc"

# -----------------------------------------------------------------------------
step "Nächtlicher Lauf und Wiederherstellungstest mit Zeitpunkt-Probe, beide Repositorys"
backup run
[ "$(base_count 1)" = 2 ] && [ "$(base_count 2)" = 2 ] || fail "nach dem Lauf werden je Repository zwei Basissicherungen erwartet"
[ "$(backup restic 1 snapshots --host pitrci --json | jq length)" = 2 ] || fail "die täglichen Stände bleiben für sich"
backup restore-test 1 mandari
grep -q "Zeitpunkt-Probe bestanden" "$T/status/restore_test_ok" || fail "die Statusdatei nennt die Zeitpunkt-Probe nicht"
backup restore-test 2 mandari
cat "$T/status/restore_test_ok"
backup pitr-wal
watchdog || fail "der Watchdog meldet einen gesunden Stand als Problem"

# -----------------------------------------------------------------------------
step "Sperre: Die WAL-Übertragung weicht aus, ein geplanter Lauf wartet, statt auszufallen"
dc exec -T -d backup flock /run/mandari-backup.lock sleep 10
sleep 1
out=$(backup pitr-wal)
echo "$out"
grep -q "folgt beim nächsten Takt" <<< "$out" || fail "die WAL-Übertragung muss einer besetzten Sperre ausweichen"
SECONDS=0
backup check
[ "$SECONDS" -ge 5 ] || fail "die Prüfung hat nicht auf die Sperre gewartet"
grep -q "geprüft" "$T/status/check_ok" || fail "die Prüfung nach dem Warten fehlt in der Statusdatei"
# Die Zeile des Zeitplans, wie crond sie ausführt
zeile=$(dc exec -T backup sh -c "grep pitr-wal /etc/crontabs/root | cut -d' ' -f6-")
before=$(docker logs "$PROJECT" 2>&1 | grep -c "WAL übertragen" || true)
dc exec -T backup sh -c "$zeile"
[ "$(docker logs "$PROJECT" 2>&1 | grep -c "WAL übertragen")" -gt "$before" ] ||
    fail "der Zeitplan der WAL-Übertragung schreibt nicht ins Protokoll des Containers"

# -----------------------------------------------------------------------------
step "mandari-backup pitr: Ortszeit des Containers, WAL aus dem lokalen Archiv"
markierungen
ortszeit=$(TZ=Europe/Berlin date -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')
out=$(backup pitr --zeitpunkt "$ortszeit" --behalten)
echo "$out"
grep -q "Zeitpunkt erreicht" <<< "$out" || fail "der Zeitpunkt wurde nicht erreicht"
grep -Eq "public\.rundlauf +20[0-9]{3}$" <<< "$out" || fail "die Zeilenzahl der Tabelle fehlt in der Ausgabe"
erwarte_stand "Ortszeit, lokales WAL"
[ ! -e "$T/work/pitr-restore" ] || fail "pitr-aufraeumen hat die Wegwerf-Instanz nicht entfernt"

step "mandari-backup pitr: Angabe in UTC, Basissicherung und WAL aus dem zweiten Repository"
backup pitr-wal
out=$(backup pitr --zeitpunkt "$(date -u -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')Z" --repo 2 --wal ablage --behalten)
echo "$out"
grep -q "Zeitpunkt erreicht" <<< "$out" || fail "der Zeitpunkt wurde nicht erreicht"
erwarte_stand "UTC, WAL aus dem Repository"

for bad in "2099-01-01 00:00" "gestern"; do
    rc=0
    backup pitr --zeitpunkt "$bad" || rc=$?
    [ "$rc" = 64 ] || fail "Zeitpunkt \"$bad\" muss abgelehnt werden (64), nicht $rc"
done

# -----------------------------------------------------------------------------
step "Aufbewahrung: Plan mit vorgestellter Uhr, dann Aufräumen mit Fenster null"
plan=$(backup pitr-plan 1)
echo "$plan"
[ "$(grep -c "behalten" <<< "$plan")" = 2 ] || fail "heute liegen beide Basissicherungen im Fenster"
plan=$(backup pitr-plan --jetzt "$(($(date +%s) + 7 * 86400 + 3600))" 1)
echo "$plan"
[ "$(grep -c "anker" <<< "$plan")" = 1 ] && [ "$(grep -c "loeschen" <<< "$plan")" = 1 ] ||
    fail "in acht Tagen bleibt die jüngste als Anker, die ältere fällt weg"

markierungen
# Diese Basissicherung zugleich unkomprimiert (BACKUP_PITR_COMPRESS=0): Auf ihr bauen die folgenden Schritte auf
out=$(dc exec -T -e BACKUP_PITR_KEEP_DAYS=0 -e BACKUP_PITR_COMPRESS=0 backup mandari-backup pitr-basis)
echo "$out"
grep -q "/base\.tar$" <<< "$out" || fail "mit BACKUP_PITR_COMPRESS=0 wird eine unkomprimierte Basissicherung erwartet"
plan=$(backup pitr-plan 1)
echo "$plan"
left=$(base_count 1)
[ "$left" -ge 1 ] && [ "$left" -le 2 ] || fail "nach dem Aufräumen bleiben ein bis zwei Basissicherungen, nicht $left"
status=$(backup pitr-status)
echo "$status"
grep -q "(läuft).*Lücke: keine" <<< "$status" || fail "die laufende WAL-Kette hat eine Lücke"
# Von der ältesten behaltenen Basissicherung an muss das WAL vollständig sein: Zurück auf
# die Sekunde ihres Endes spielt alles ein, was seither gebraucht wird.
oldest=$(sed -n 's/.*Basissicherung fertig \([0-9T:-]*Z\),.*/\1/p' <<< "$plan" | head -n 1)
oldest="${oldest/T/ }"
markierungen
for source in lokal ablage; do
    if [ "$source" = ablage ]; then backup pitr-wal; fi
    out=$(backup pitr --zeitpunkt "$oldest" --wal "$source")
    echo "$out"
    grep -q "Zeitpunkt erreicht" <<< "$out" || fail "ab der ältesten behaltenen Basissicherung fehlt WAL ($source)"
    out=$(backup pitr --zeitpunkt "$(date -u -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')Z" --wal "$source" --behalten)
    grep -q "Zeitpunkt erreicht" <<< "$out" || fail "nach dem Aufräumen wird der Zeitpunkt nicht erreicht ($source)"
    erwarte_stand "nach dem Aufräumen, WAL $source"
done

# -----------------------------------------------------------------------------
step "WAL-Empfänger: Neustart nach Abbruch, dieselbe Kette, keine Lücke"
dc exec -T backup pkill -KILL -x pg_receivewal
wait_for 30 "Abbruch bemerkt" sh -c "docker logs $PROJECT 2>&1 | grep -q 'WAL-Empfang endete mit Code'"
wait_for 60 "Empfänger läuft wieder" slot_active
[ "$(chain)" = "$KETTE" ] || fail "ein Neustart des Empfängers darf keine neue Kette beginnen"
markierungen
out=$(backup pitr --zeitpunkt "$(date -u -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')Z" --behalten)
grep -q "Zeitpunkt erreicht" <<< "$out" || fail "nach dem Neustart des Empfängers wird der Zeitpunkt nicht erreicht"
erwarte_stand "nach Neustart des Empfängers"
grep -q "Lücke: keine" <<< "$(backup pitr-status | grep "(läuft)")" || fail "Lücke nach dem Neustart des Empfängers"

# -----------------------------------------------------------------------------
step "Watchdog: gestauter Slot, alte Basissicherung"
dc exec -T backup pkill -STOP -x pg_receivewal
db_sql "insert into rundlauf (id, marke) select g, 'stau' from generate_series(100001, 300000) g" >/dev/null
rc=0
dc exec -T -e BACKUP_PITR_SLOT_WARN_MB=1 backup mandari-backup pitr-wal || rc=$?
dc exec -T backup pkill -CONT -x pg_receivewal
[ "$rc" != 0 ] || fail "ein gestauter Slot muss als Fehler enden"
grep -q "staut" "$T/status/pitr_empfang_failed" || fail "die Statusdatei nennt den Stau nicht"
out=$(watchdog) && fail "der Watchdog muss den gestauten Slot melden"
grep -q "PITR: .*staut" <<< "$out" || fail "die Meldung des Watchdogs nennt den Stau nicht: $out"
wait_for 60 "Empfänger läuft wieder" slot_active
backup pitr-wal
watchdog || fail "nach dem Abbau des Staus meldet der Watchdog weiter ein Problem"

sudo touch -d "27 hours ago" "$T/status/pitr_basis_last_success"
out=$(watchdog) && fail "der Watchdog muss die alte Basissicherung melden"
grep -q "PITR: Die jüngste Basissicherung ist 27 Stunden alt" <<< "$out" || fail "Meldung zur alten Basissicherung fehlt: $out"
sudo touch "$T/status/pitr_basis_last_success"

# -----------------------------------------------------------------------------
step "Neue WAL-Kette auf Anforderung: Basissicherung folgt von selbst, die alte Kette bleibt nutzbar"
markierungen
backup pitr-neue-kette
wait_for 90 "neue Kette" sh -c "[ \"\$(docker exec $PROJECT cat /pitr/wal/aktuell)\" != \"$KETTE\" ]"
NEUE_KETTE=$(chain)
wait_for 180 "Basissicherung der neuen Kette" dc exec -T backup test -e "/pitr/wal/$NEUE_KETTE/.basis"
wait_for 60 "Basissicherung abgeschlossen" sh -c "grep -q 'Kette $NEUE_KETTE' '$T/status/pitr_basis_last_success'"
out=$(watchdog) && fail "der Watchdog muss die unterbrochene Kette melden"
grep -q "PITR: .*WAL-Kette $KETTE endet" <<< "$out" || fail "Meldung zur unterbrochenen Kette fehlt: $out"
sudo rm -f "$T/status/pitr_kette_unterbrochen"
out=$(backup pitr --zeitpunkt "$(date -u -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')Z" --behalten)
grep -q "Zeitpunkt erreicht" <<< "$out" || fail "ein Zeitpunkt in der alten Kette wird nicht mehr erreicht"
erwarte_stand "Zeitpunkt in der alten Kette"
markierungen
out=$(backup pitr --zeitpunkt "$(date -u -d "@$ZIEL" '+%Y-%m-%d %H:%M:%S')Z" --behalten)
grep -q "Kette $NEUE_KETTE" <<< "$out" || fail "die Wiederherstellung nutzt nicht die Basissicherung der neuen Kette"
erwarte_stand "Zeitpunkt in der neuen Kette"

# -----------------------------------------------------------------------------
step "Datenbank weg: Stand bis zur letzten Transaktion aus dem angefangenen Segment"
db_sql "insert into rundlauf (id, marke) values (99999, 'zuletzt')" >/dev/null
sleep 3
docker stop "$DB" >/dev/null
out=$(backup pitr --zeitpunkt ende --behalten)
echo "$out"
grep -q "Angefangenes Segment" <<< "$out" || fail "das angefangene Segment wurde nicht einbezogen"
grep -q "Ende des WAL-Archivs erreicht" <<< "$out" || fail "das Ende des Archivs wurde nicht erreicht"
[ "$(restored_sql "select marke from rundlauf where id = 99999")" = zuletzt ] ||
    fail "die letzte Transaktion vor dem Ausfall fehlt"
backup pitr-aufraeumen
# Ein Zeitpunkt nach der letzten Transaktion: Stand bis zum Ende des Archivs, aber kein Erfolg
rc=0
out=$(backup pitr --zeitpunkt "$(TZ=Europe/Berlin date '+%Y-%m-%d %H:%M:%S')") || rc=$?
echo "$out"
[ "$rc" = 2 ] || fail "endet das Archiv vor dem Zeitpunkt, ist der Rückgabewert 2, nicht $rc"
grep -q "ACHTUNG: Das WAL-Archiv endet vor dem Zeitpunkt" <<< "$out" || fail "der Hinweis auf das Ende des Archivs fehlt"

step "Watchdog: stehender WAL-Empfang"
rc=0
backup pitr-wal || rc=$?
[ "$rc" != 0 ] || fail "ohne Datenbank muss die Empfangsprüfung scheitern"
out=$(watchdog) && fail "der Watchdog muss den stehenden WAL-Empfang melden"
grep -q "PITR: .*WAL-Empfang steht" <<< "$out" || fail "Meldung zum stehenden WAL-Empfang fehlt: $out"
docker start "$DB" >/dev/null
wait_for 120 "Empfänger nach dem Neustart der Datenbank" slot_active
[ "$(chain)" = "$NEUE_KETTE" ] || fail "ein Neustart der Datenbank darf keine neue Kette beginnen"
backup pitr-wal
watchdog || fail "nach dem Neustart der Datenbank meldet der Watchdog weiter ein Problem"

# -----------------------------------------------------------------------------
step "Sauberes Ende"
dc stop --timeout 20 backup
[ "$(docker inspect -f '{{.State.ExitCode}}' "$PROJECT")" = 0 ] || fail "der Container endet nicht mit 0"
docker logs "$PROJECT" 2>&1 | grep -q "=== WAL-Empfang beendet" || fail "der WAL-Empfänger wurde nicht sauber beendet"
[ "$(db_sql "select active from pg_replication_slots where slot_name = '$ROLE'" postgres)" = f ] ||
    fail "der Slot ist nach dem Ende noch belegt"

step "Rundlauf bestanden"
