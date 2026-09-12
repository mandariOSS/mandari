#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# mandari-backup – verschlüsselte Offsite-Sicherung mit restic (im Container)
# =============================================================================
#   mandari-backup init                   Repositorys anlegen, falls nicht vorhanden
#   mandari-backup run                    Dumps erstellen, sichern, alte Stände löschen
#   mandari-backup check                  Integrität prüfen (rotierend 1/4 der Daten)
#   mandari-backup restore-test [N] [DB]  Wiederherstellung in Wegwerf-Datenbank prüfen
#   mandari-backup snapshots              Sicherungsstände aller Repositorys
#   mandari-backup restic N ARGS…         restic direkt gegen Repository N aufrufen
#
# Konfiguration über Umgebungsvariablen (siehe deploy/backup/.env.example).
# Statusdateien in /status werden vom Host-Skript backup-watchdog.sh ausgewertet.
# =============================================================================
set -uo pipefail

# Cron-Jobs erhalten die Container-Umgebung nicht; der Entrypoint legt sie ab.
if [ -z "${RESTIC_REPO_1:-}" ] && [ -f /run/mandari-backup.env ]; then
    # shellcheck disable=SC1091
    . /run/mandari-backup.env
fi

STATUS_DIR=/status
WORK_DIR=/work
DUMP_DIR="$WORK_DIR/dumps"
HOST_TAG="${BACKUP_HOSTNAME:-mandari}"
KEEP_WITHIN_DAILY="${BACKUP_KEEP_WITHIN_DAILY:-30d}"
RESTORE_TEST_DB="${BACKUP_RESTORE_TEST_DB:-mandari}"
TOLERANCE_PERCENT=5
SAMPLE_FILES=20
LOCK_FILE=/run/mandari-backup.lock

export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/run/secrets/restic_password}"
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-/root/.cache/restic}"
export PGHOST="${BACKUP_PG_HOST:-mandari-postgres}"
export PGUSER="${BACKUP_PG_USER:-mandari}"

SSH_ARGS="-i /root/.ssh/id_ed25519 -o UserKnownHostsFile=/root/.ssh/known_hosts -o StrictHostKeyChecking=yes"
SSH_ARGS="$SSH_ARGS -o BatchMode=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=6"
if ssh -G -o WarnWeakCrypto=no localhost >/dev/null 2>&1; then
    SSH_ARGS="$SSH_ARGS -o WarnWeakCrypto=no"
fi

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

repo_numbers() {
    local n var
    for n in 1 2 3; do
        var="RESTIC_REPO_$n"
        if [ -n "${!var:-}" ]; then echo "$n"; fi
    done
}
repo_url() { local var="RESTIC_REPO_$1"; echo "${!var:-}"; }
repo_label() { local var="RESTIC_REPO_${1}_LABEL"; echo "${!var:-repo-$1}"; }

repo() {
    local n="$1"
    shift
    restic -r "$(repo_url "$n")" -o sftp.args="$SSH_ARGS" "$@"
}

take_lock() {
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "Ein anderer Backup-Vorgang läuft noch – Abbruch."
        exit 75
    fi
}

cmd_init() {
    local n label
    for n in $(repo_numbers); do
        label=$(repo_label "$n")
        if repo "$n" cat config >/dev/null 2>&1; then
            log "[$label] Repository vorhanden"
        else
            log "[$label] Repository wird angelegt"
            repo "$n" init || return 1
        fi
    done
}

dump_databases() {
    local databases db
    rm -rf "$DUMP_DIR" && mkdir -p "$DUMP_DIR" && chmod 700 "$DUMP_DIR" || return 1
    databases=$(psql -d postgres -Atc \
        "select datname from pg_database where datallowconn and not datistemplate order by datname") || return 1
    log "  pg_dumpall --globals-only"
    pg_dumpall --globals-only -f "$DUMP_DIR/globals.sql" || return 1
    for db in $databases; do
        log "  pg_dump $db"
        # Unkomprimiert: restic dedupliziert und komprimiert selbst
        pg_dump -d "$db" --format=custom --compress=0 -f "$DUMP_DIR/$db.dump" || return 1
    done
    du -sh "$DUMP_DIR"/* | sed 's/^/  /'
}

cmd_run() {
    take_lock
    local started n label failed=()
    started=$(date +%s)
    log "=== Sicherung startet (Stand $HOST_TAG)"

    if ! dump_databases; then
        echo "$(now_utc) Datenbank-Dump fehlgeschlagen" > "$STATUS_DIR/last_error"
        log "FEHLER: Datenbank-Dump fehlgeschlagen"
        rm -rf "$DUMP_DIR"
        return 1
    fi

    for n in $(repo_numbers); do
        label=$(repo_label "$n")
        log "[$label] Sicherung"
        if ! repo "$n" backup /source "$DUMP_DIR" --host "$HOST_TAG" --tag daily \
            --exclude-caches --exclude-if-present .nobackup \
            --exclude /source/config/backups --exclude /source/config/logs \
            --exclude '**/node_modules' --exclude '**/__pycache__'; then
            failed+=("$label (Sicherung)")
            continue
        fi
        log "[$label] Stände älter als $KEEP_WITHIN_DAILY entfernen"
        if ! repo "$n" forget --host "$HOST_TAG" --keep-within-daily "$KEEP_WITHIN_DAILY" --prune; then
            failed+=("$label (Aufräumen)")
            continue
        fi
        now_utc > "$STATUS_DIR/repo-$label.last_success"
    done
    rm -rf "$DUMP_DIR"

    if [ ${#failed[@]} -gt 0 ]; then
        echo "$(now_utc) Sicherung fehlgeschlagen: ${failed[*]}" > "$STATUS_DIR/last_error"
        log "FEHLER: ${failed[*]}"
        return 1
    fi
    now_utc > "$STATUS_DIR/last_success"
    rm -f "$STATUS_DIR/last_error"
    log "=== Sicherung erfolgreich nach $(( $(date +%s) - started )) s"
}

cmd_check() {
    take_lock
    local subset n label failed=()
    subset="$(( 10#$(date +%V) % 4 + 1 ))/4"
    for n in $(repo_numbers); do
        label=$(repo_label "$n")
        log "[$label] Integritätsprüfung, Datenteil $subset"
        repo "$n" check --read-data-subset="$subset" || failed+=("$label")
    done
    if [ ${#failed[@]} -gt 0 ]; then
        echo "$(now_utc) Integritätsprüfung fehlgeschlagen: ${failed[*]}" > "$STATUS_DIR/check_failed"
        log "FEHLER: Integritätsprüfung ${failed[*]}"
        return 1
    fi
    rm -f "$STATUS_DIR/check_failed"
    echo "$(now_utc) Datenteil $subset aller Repositorys geprüft" > "$STATUS_DIR/check_ok"
}

cmd_restore_test() {
    take_lock
    local n="${1:-}" db="${2:-$RESTORE_TEST_DB}" label started T dump table prod restored low high
    local checked=0 compared=0 tables sample file expected actual
    if [ -z "$n" ]; then
        if [ $(( 10#$(date +%m) % 2 )) -eq 1 ] || [ -z "$(repo_url 2)" ]; then n=1; else n=2; fi
    fi
    label=$(repo_label "$n")
    started=$(date +%s)
    T="$WORK_DIR/restore-test"
    local P=(-h "$T/run" -p 55432 -U "$PGUSER")

    restore_cleanup() {
        if [ -f "$T/pgdata/postmaster.pid" ]; then
            su-exec postgres pg_ctl -D "$T/pgdata" -m fast -w stop >/dev/null 2>&1
        fi
        rm -rf "$T"
    }
    restore_fail() {
        log "FEHLER: $*"
        echo "$(now_utc) Repository $label: $*" > "$STATUS_DIR/restore_test_failed"
        restore_cleanup
        exit 1
    }

    log "=== Wiederherstellungstest – Repository $label, Datenbank $db"
    rm -rf "$T"
    mkdir -p "$T/extract" "$T/run"

    repo "$n" restore latest --host "$HOST_TAG" --target "$T/extract" \
        --include "$DUMP_DIR/globals.sql" --include "$DUMP_DIR/$db.dump" ||
        restore_fail "restic restore fehlgeschlagen"
    dump="$T/extract$DUMP_DIR/$db.dump"
    [ -s "$dump" ] || restore_fail "Dump $db.dump fehlt im neuesten Sicherungsstand"

    # Die Wegwerf-Datenbank läuft als Benutzer postgres: Arbeitsverzeichnis nur
    # durchquerbar machen (nicht lesbar); die Dumps bleiben in dumps/ root-only.
    chmod 711 "$WORK_DIR"
    chown -R postgres:postgres "$T"
    su-exec postgres initdb -D "$T/pgdata" -U "$PGUSER" --auth=trust --no-locale --encoding=UTF8 >/dev/null ||
        restore_fail "initdb fehlgeschlagen"
    su-exec postgres pg_ctl -D "$T/pgdata" -l "$T/postgres.log" -w \
        -o "-p 55432 -c listen_addresses= -c unix_socket_directories=$T/run -c fsync=off -c full_page_writes=off -c max_wal_size=4GB" \
        start >/dev/null || restore_fail "Test-Datenbank startet nicht"

    # Die bereits vorhandene Superuser-Rolle erzeugt hier erwartbare Fehler
    psql "${P[@]}" -d postgres -q -f "$T/extract$DUMP_DIR/globals.sql" >/dev/null 2>&1
    createdb "${P[@]}" "$db" || restore_fail "Datenbank $db nicht anlegbar"
    log "  pg_restore $db"
    if ! pg_restore "${P[@]}" -d "$db" --jobs=2 "$dump" 2> "$T/pg_restore.err"; then
        tail -20 "$T/pg_restore.err"
        restore_fail "pg_restore meldet Fehler: $(tail -1 "$T/pg_restore.err")"
    fi
    log "  Datenbank nach $(( $(date +%s) - started )) s wiederhergestellt"

    log "  Zeilenzahlen der größten Tabellen (Toleranz $TOLERANCE_PERCENT %)"
    tables=$(psql -d "$db" -Atc \
        "select format('%I.%I', schemaname, relname) from pg_stat_user_tables order by n_live_tup desc limit 8") ||
        restore_fail "Tabellenliste der Produktion nicht lesbar"
    [ -n "$tables" ] || restore_fail "Keine Tabellen in der Produktion gefunden"
    while IFS= read -r table; do
        prod=$(psql -d "$db" -Atc "select count(*) from $table") || restore_fail "Zählung $table (Produktion) fehlgeschlagen"
        restored=$(psql "${P[@]}" -d "$db" -Atc "select count(*) from $table") ||
            restore_fail "Tabelle $table fehlt in der Wiederherstellung"
        printf '    %-50s Produktion %10s   wiederhergestellt %10s\n' "$table" "$prod" "$restored"
        low=$(( prod * (100 - TOLERANCE_PERCENT) / 100 ))
        high=$(( prod * (100 + TOLERANCE_PERCENT) / 100 + 10 ))
        if [ "$restored" -lt "$low" ] || [ "$restored" -gt "$high" ]; then
            restore_fail "Tabelle $table: $restored Zeilen wiederhergestellt, Produktion $prod"
        fi
        checked=$((checked + 1))
    done <<< "$tables"

    log "  Stichprobe: $SAMPLE_FILES Dateien (unverändert seit über 25 Stunden)"
    sample=$(find /source/files /source/media -type f -mmin +1500 2>/dev/null | shuf -n "$SAMPLE_FILES")
    while IFS= read -r file; do
        [ -n "$file" ] || continue
        expected=$(sha256sum < "$file" | cut -d' ' -f1)
        actual=$(repo "$n" dump --host "$HOST_TAG" latest "$file" 2>/dev/null | sha256sum | cut -d' ' -f1)
        [ "$expected" = "$actual" ] || restore_fail "Prüfsumme weicht ab: $file"
        compared=$((compared + 1))
    done <<< "$sample"

    restore_cleanup
    echo "$(now_utc) Repository $label: Datenbank $db ($checked Tabellen) und $compared Dateien geprüft," \
        "Dauer $(( $(date +%s) - started )) s" > "$STATUS_DIR/restore_test_ok"
    rm -f "$STATUS_DIR/restore_test_failed"
    log "=== Wiederherstellungstest erfolgreich: $(cat "$STATUS_DIR/restore_test_ok")"
}

cmd_snapshots() {
    local n
    for n in $(repo_numbers); do
        log "=== $(repo_label "$n")"
        repo "$n" snapshots --compact
        repo "$n" stats --mode raw-data | tail -n +2
    done
}

usage() {
    sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//'
}

mkdir -p "$STATUS_DIR" "$WORK_DIR"

case "${1:-}" in
    init) cmd_init ;;
    run) cmd_run ;;
    check) cmd_check ;;
    restore-test) shift; cmd_restore_test "$@" ;;
    snapshots) cmd_snapshots ;;
    restic)
        shift
        [ $# -ge 1 ] || { usage; exit 64; }
        number="$1"
        shift
        repo "$number" "$@"
        ;;
    *) usage; exit 64 ;;
esac
