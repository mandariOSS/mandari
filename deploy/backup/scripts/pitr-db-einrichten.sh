#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# Datenbank für die Wiederherstellung auf einen Zeitpunkt vorbereiten
# (auf dem Host, als root; einmal vor BACKUP_PITR=1)
# =============================================================================
# Richtet im PostgreSQL-Container ein, was der Backup-Container für den WAL-Empfang
# und die Basissicherung braucht. Werte aus der .env des Backup-Stacks:
#   1. Rolle BACKUP_PITR_REPL_USER mit REPLICATION und dem Passwort
#      BACKUP_PITR_REPL_PASSWORD (als SCRAM-Kennwort gespeichert)
#   2. eine Zeile in pg_hba.conf: Replikationsverbindungen dieser Rolle aus den
#      Netzen des Datenbank-Containers (samenet), nur mit Passwort (scram-sha-256)
#   3. max_slot_wal_keep_size = BACKUP_PITR_SLOT_MAX_WAL: Steht der Empfänger, hält
#      die Datenbank höchstens so viel WAL für ihn zurück
# Alles wirkt mit dem Neuladen der Konfiguration, ohne Neustart der Datenbank. Einen
# Neustart braucht es nur, wenn wal_level, max_wal_senders oder max_replication_slots
# unter die Standardwerte gesetzt wurden; das Skript prüft das und nennt den Wert.
# Ein zweiter Aufruf ändert nichts mehr; das Passwort wird jedes Mal aus der .env gesetzt.
#
#   scripts/pitr-db-einrichten.sh            # einrichten und prüfen
#   scripts/pitr-db-einrichten.sh --pruefen  # nur prüfen, nichts ändern
# =============================================================================
set -uo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

env_value() {
    grep -E "^$1=" "$BACKUP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/'
}

PG_CONTAINER="$(env_value BACKUP_PG_CONTAINER)"
PG_CONTAINER="${PG_CONTAINER:-$(env_value BACKUP_PG_HOST)}"
PG_CONTAINER="${PG_CONTAINER:-mandari-postgres}"
PG_USER="$(env_value BACKUP_PG_USER)"; PG_USER="${PG_USER:-mandari}"
ROLE="$(env_value BACKUP_PITR_REPL_USER)"; ROLE="${ROLE:-mandari_pitr}"
PASSWORD="$(env_value BACKUP_PITR_REPL_PASSWORD)"
MAX_WAL="$(env_value BACKUP_PITR_SLOT_MAX_WAL)"; MAX_WAL="${MAX_WAL:-4GB}"
HBA_LINE="host    replication     $ROLE    samenet    scram-sha-256"

check_only=0
case "${1:-}" in
    "") ;;
    --pruefen) check_only=1 ;;
    *)
        sed -n '4,21p' "$0" | sed 's/^# \{0,1\}//'
        exit 64
        ;;
esac

fail() {
    echo "FEHLER: $*" >&2
    exit 1
}

[[ "$ROLE" =~ ^[a-z_][a-z0-9_]*$ ]] || fail "BACKUP_PITR_REPL_USER darf nur Kleinbuchstaben, Ziffern und _ enthalten"
[[ "$MAX_WAL" =~ ^[0-9]+(MB|GB|TB)$ ]] || fail "BACKUP_PITR_SLOT_MAX_WAL erwartet eine Größe wie 4GB"
[ -n "$PASSWORD" ] || fail "BACKUP_PITR_REPL_PASSWORD fehlt in $BACKUP_DIR/.env (erzeugen: openssl rand -hex 24)"
[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = true ] ||
    fail "Der PostgreSQL-Container $PG_CONTAINER läuft nicht (BACKUP_PG_CONTAINER in der .env setzen)"

# psql im Datenbank-Container über den lokalen Socket; Werte kommen als Umgebung an,
# nie in der Befehlszeile
pg() {
    PITR_ROLE="$ROLE" PITR_PASSWORD="$PASSWORD" PITR_MAX_WAL="$MAX_WAL" PITR_HBA_LINE="$HBA_LINE" PITR_PG_USER="$PG_USER" \
        docker exec -i -e PITR_ROLE -e PITR_PASSWORD -e PITR_MAX_WAL -e PITR_HBA_LINE -e PITR_PG_USER "$PG_CONTAINER" "$@"
}
sql() { pg psql -X -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -At "$@"; }

version=$(sql -c "show server_version_num") || fail "Keine Anmeldung als $PG_USER im Container $PG_CONTAINER"
[ "$version" -ge 130000 ] || fail "PostgreSQL 13 oder neuer nötig (max_slot_wal_keep_size), gefunden: $version"

changed=0
note() { echo "  $*"; }

# --- 1. Rolle ---------------------------------------------------------------
role_state=$(sql -c "select case when rolreplication and rolcanlogin then 'bereit' else 'unvollständig' end
    from pg_roles where rolname = '$ROLE'") || fail "Rollen nicht lesbar"
if [ "$check_only" -eq 1 ]; then
    note "Rolle $ROLE: ${role_state:-fehlt}"
else
    # Das Passwort soll weder im Protokoll der Datenbank noch in einer Prozessliste stehen
    sql -q <<'SQL' || fail "Rolle nicht anlegbar"
\set role `printenv PITR_ROLE`
\set pw `printenv PITR_PASSWORD`
set log_statement = 'none';
set log_min_duration_statement = -1;
set log_min_error_statement = 'panic';
set password_encryption = 'scram-sha-256';
select format('create role %I login replication', :'role')
 where not exists (select 1 from pg_roles where rolname = :'role') \gexec
alter role :"role" with login replication password :'pw';
SQL
    if [ "$role_state" = bereit ]; then
        note "Rolle $ROLE: vorhanden, Passwort aus der .env gesetzt"
    else
        note "Rolle $ROLE: angelegt (REPLICATION, LOGIN)"
        changed=1
    fi
fi

# --- 2. pg_hba.conf ---------------------------------------------------------
# shellcheck disable=SC2016  # läuft in der Shell des Containers
hba_state=$(pg sh -c '
    file=$(psql -X -U "$PITR_PG_USER" -d postgres -Atc "show hba_file") || exit 1
    if grep -Eq "^[[:space:]]*host(ssl|nossl)?[[:space:]]+replication[[:space:]]+$PITR_ROLE[[:space:]]" "$file"; then
        echo vorhanden
    elif [ "$1" = 1 ]; then
        echo fehlt
    else
        printf "\n# mandari-backup: WAL-Empfang und Basissicherung (PITR)\n%s\n" "$PITR_HBA_LINE" >> "$file" &&
            echo ergänzt
    fi' sh "$check_only") || fail "pg_hba.conf nicht lesbar oder nicht schreibbar"
note "pg_hba.conf, Zeile für $ROLE: $hba_state"
if [ "$hba_state" = ergänzt ]; then changed=1; fi

# --- 3. max_slot_wal_keep_size ----------------------------------------------
wal_state=$(sql <<'SQL'
\set want `printenv PITR_MAX_WAL`
select case when setting::bigint = pg_size_bytes(:'want') / 1048576 then 'gleich' else 'anders' end
  from pg_settings where name = 'max_slot_wal_keep_size';
SQL
) || fail "max_slot_wal_keep_size nicht lesbar"
if [ "$wal_state" = gleich ]; then
    note "max_slot_wal_keep_size: $MAX_WAL"
elif [ "$check_only" -eq 1 ]; then
    note "max_slot_wal_keep_size: $(sql -c "show max_slot_wal_keep_size") (gewünscht $MAX_WAL)"
else
    sql -q <<'SQL' || fail "max_slot_wal_keep_size nicht setzbar"
\set want `printenv PITR_MAX_WAL`
alter system set max_slot_wal_keep_size = :'want';
SQL
    note "max_slot_wal_keep_size: auf $MAX_WAL gesetzt"
    changed=1
fi

if [ "$check_only" -eq 0 ]; then
    sql -q -c "select pg_reload_conf()" >/dev/null || fail "Konfiguration nicht neu ladbar"
    sleep 2
    if [ "$changed" -eq 1 ]; then note "Konfiguration neu geladen (kein Neustart)"; fi
fi

# --- Prüfung ----------------------------------------------------------------
problems=0
problem() {
    echo "  FEHLT: $*" >&2
    problems=$((problems + 1))
}

read -r wal_level senders slots hba_errors <<< "$(sql -F ' ' -c "select current_setting('wal_level'),
    current_setting('max_wal_senders'), current_setting('max_replication_slots'),
    (select count(*) from pg_hba_file_rules where error is not null)")"
case "$wal_level" in
    replica | logical) ;;
    *) problem "wal_level ist \"$wal_level\"; nötig ist replica (Standard). Die Änderung braucht einen Neustart der Datenbank." ;;
esac
# Ein Empfänger, dazu zwei Verbindungen während der Basissicherung
[ "${senders:-0}" -ge 3 ] ||
    problem "max_wal_senders ist ${senders:-?}; nötig sind mindestens 3 (Standard 10). Die Änderung braucht einen Neustart der Datenbank."
[ "${slots:-0}" -ge 1 ] ||
    problem "max_replication_slots ist ${slots:-?}; nötig ist mindestens 1 (Standard 10). Die Änderung braucht einen Neustart der Datenbank."
[ "${hba_errors:-1}" = 0 ] || problem "pg_hba.conf enthält fehlerhafte Zeilen (select * from pg_hba_file_rules where error is not null)"
if [ "$(sql -c "show max_slot_wal_keep_size")" = -1 ]; then
    problem "max_slot_wal_keep_size ist unbegrenzt: Steht der Empfänger, läuft der Datenträger der Datenbank voll"
fi

# Die Anmeldung, wie der Backup-Container sie braucht: über das Netz, mit Passwort,
# als Replikationsverbindung
# shellcheck disable=SC2016  # läuft in der Shell des Containers
if pg sh -c 'PGPASSWORD="$PITR_PASSWORD" PGCONNECT_TIMEOUT=10 psql -X -Atq \
        "host=$(hostname -i | cut -d" " -f1) user=$PITR_ROLE dbname=postgres replication=true" \
        -c "IDENTIFY_SYSTEM" >/dev/null' 2>/dev/null; then
    note "Replikationsanmeldung als $ROLE über das Netz: gelingt"
else
    problem "Replikationsanmeldung als $ROLE über das Netz scheitert (Rolle, Passwort oder pg_hba.conf)"
fi

if [ "$problems" -gt 0 ]; then
    echo "Die Datenbank ist für BACKUP_PITR=1 noch nicht bereit ($problems Punkte)." >&2
    exit 1
fi
echo "Die Datenbank ist für BACKUP_PITR=1 bereit."
