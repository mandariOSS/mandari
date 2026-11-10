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
# Zurück auf einen Zeitpunkt (Point-in-Time Recovery, eingeschaltet mit BACKUP_PITR=1):
#   mandari-backup pitr --zeitpunkt "JJJJ-MM-TT HH:MM[:SS]" [--repo N] [--wal lokal|ablage]
#                       [--db NAME] [--behalten]
#                                         Stand zum Zeitpunkt in einer Wegwerf-Instanz aufbauen;
#                                         Uhrzeit in der Zeitzone des Containers (BACKUP_TZ),
#                                         wahlweise mit Z oder ±HH:MM; "ende" = alles Archivierte
#   mandari-backup pitr-aufraeumen        Wegwerf-Instanz beenden und löschen
#   mandari-backup pitr-status            WAL-Empfang, Ketten, Basissicherungen je Repository
#   mandari-backup pitr-basis             Basissicherung sofort erstellen und übertragen
#   mandari-backup pitr-wal               WAL-Archiv sofort übertragen
#   mandari-backup pitr-plan [--jetzt S] [N]  Aufbewahrung anzeigen, ohne zu löschen
#   mandari-backup pitr-neue-kette        WAL-Empfang mit neuer Kette und neuem Slot beginnen
#   mandari-backup pitr-empfang           WAL-Empfänger (startet der Container selbst)
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

# Point-in-Time Recovery (Issue #1303), nur wirksam mit BACKUP_PITR=1
PITR_WAL_DIR=/pitr/wal
PITR_BASE_DIR=/pitr/basis
PITR_RESTORE_DIR="$WORK_DIR/pitr-restore"
PITR_HOST_TAG="$HOST_TAG-pitr"
PITR_SLOT="${BACKUP_PITR_SLOT:-mandari_pitr}"
PITR_REPL_USER="${BACKUP_PITR_REPL_USER:-mandari_pitr}"
PITR_KEEP_DAYS="${BACKUP_PITR_KEEP_DAYS:-7}"
PITR_COMPRESS="${BACKUP_PITR_COMPRESS:-6}"
PITR_CHECKPOINT="${BACKUP_PITR_CHECKPOINT:-spread}"
PITR_SLOT_WARN_MB="${BACKUP_PITR_SLOT_WARN_MB:-512}"
PITR_RECHECK_SECONDS="${BACKUP_PITR_RECHECK_SECONDS:-30}"
PITR_PORT=55433
PITR_RESTORE_TIMEOUT=21600
PITR_LOCK_FILE=/run/mandari-backup-pitr.lock
PITR_RECEIVE_LOCK_FILE=/run/mandari-backup-pitr-empfang.lock
PITR_P=(-h "$PITR_RESTORE_DIR/run" -p "$PITR_PORT")
# Namen von WAL-Segmenten und Ketten werden als Zeichenketten verglichen
export LC_COLLATE=C

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

# Abgebrochene Läufe (z. B. Neustart des Containers) hinterlassen restic-Sperren.
# "restic unlock" entfernt nur veraltete Sperren: gleicher Host mit beendetem
# Prozess oder älter als 30 Minuten. Eigene parallele Läufe schließt flock aus.
release_stale_locks() {
    local n
    for n in $(repo_numbers); do
        repo "$n" unlock >/dev/null 2>&1 || true
    done
}

# Mit PITR überträgt ein kurzer Lauf alle paar Minuten das WAL. Trifft ein geplanter
# Lauf (Sicherung, Prüfung, Test) auf ihn, wartet er, statt auszufallen; ohne PITR
# bricht er wie bisher sofort ab.
take_lock() {
    local wait=0 waited=0
    if pitr_enabled; then wait=1800; fi
    exec 9>"$LOCK_FILE"
    until flock -n 9; do
        if [ "$waited" -ge "$wait" ]; then
            log "Ein anderer Backup-Vorgang läuft noch – Abbruch."
            exit 75
        fi
        sleep 5
        waited=$((waited + 5))
    done
    release_stale_locks
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
    local rc=0
    run_daily || rc=1
    # Die Basissicherung hängt nicht am Dump und läuft auch nach dessen Fehler
    if pitr_enabled; then
        pitr_base_backup || rc=1
    fi
    return "$rc"
}

run_daily() {
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

    # Ereignistechnik (Issue #573): Cursor und Sequenz passen zum Journal derselben Sicherung
    if [ "$(psql "${P[@]}" -d "$db" -Atc "select to_regclass('public.events_event') is not null")" = t ]; then
        local journal head sequence ahead tasks
        read -r journal head sequence ahead tasks <<< "$(psql "${P[@]}" -d "$db" -At -F ' ' -c "
            select count(*), coalesce(max(seq), 0), (select last_value from events_seq),
                   (select count(*) from events_subscription
                     where cursor_seq > (select coalesce(max(seq), 0) from events_event)),
                   (select count(*) from events_task)
              from events_event")" || restore_fail "Journal der Ereignistechnik nicht lesbar"
        log "  Journal: $journal Ereignisse bis Folgenummer $head, Sequenz $sequence, Aufträge $tasks"
        [ "$ahead" = 0 ] || restore_fail "Journal: $ahead Abonnements mit Cursor hinter dem Ende des Journals"
        [ "$sequence" -ge "$head" ] || restore_fail "Journal: Sequenz $sequence unter der höchsten Folgenummer $head"
    fi

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
    local probe=""
    if pitr_enabled; then
        pitr_probe "$n" || restore_fail "Zeitpunkt-Probe: $PITR_ERROR"
        probe=", $PITR_PROBE_RESULT"
    fi
    echo "$(now_utc) Repository $label: Datenbank $db ($checked Tabellen) und $compared Dateien geprüft$probe," \
        "Dauer $(( $(date +%s) - started )) s" > "$STATUS_DIR/restore_test_ok"
    rm -f "$STATUS_DIR/restore_test_failed"
    log "=== Wiederherstellungstest erfolgreich: $(cat "$STATUS_DIR/restore_test_ok")"
}

# =============================================================================
# Point-in-Time Recovery (Issue #1303): Basissicherung und fortlaufendes WAL
# =============================================================================
# /pitr/wal/<kette>/   WAL-Segmente von pg_receivewal (gzip), je Kette lückenlos;
#                      .kennung = Systemkennung und Segmentgröße der Datenbank,
#                      .basis   = zu dieser Kette gibt es eine Basissicherung
# /pitr/wal/aktuell    Name der laufenden Kette
# /pitr/basis/         Zwischenablage der Basissicherung bis zur Übertragung
# Eine neue Kette beginnt, wenn sich das WAL nicht lückenlos fortsetzen lässt:
# anderer Datenbank-Cluster, Slot fehlt oder hat WAL verloren, pitr-neue-kette.
# In den Repositorys liegen die Stände unter dem Namen <BACKUP_HOSTNAME>-pitr;
# Aufbewahrung und Wiederherstellungstest der täglichen Stände sehen sie nicht.

pitr_enabled() { [ "${BACKUP_PITR:-0}" = 1 ]; }

pitr_require_enabled() {
    if ! pitr_enabled; then
        log "PITR ist ausgeschaltet (BACKUP_PITR=1 in der .env schaltet es ein)."
        exit 64
    fi
}

# Verbindungen über das Replikationsprotokoll: eigene Rolle, eigenes Passwort
repl() { PGUSER="$PITR_REPL_USER" PGPASSWORD="${BACKUP_PITR_REPL_PASSWORD:-}" "$@"; }
pitr_psql() { PGCONNECT_TIMEOUT=10 psql -X -d postgres -At "$@"; }
pitr_chain() { cat "$PITR_WAL_DIR/aktuell" 2>/dev/null; }
pitr_utc() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }

pitr_segsize() {
    local size
    size=$(cut -d' ' -f2 "$1/.kennung" 2>/dev/null)
    echo "${size:-16777216}"
}

# "aktiv wal_status rückstand_in_bytes" des Slots; leer, wenn es ihn nicht gibt
pitr_slot_state() {
    pitr_psql -F ' ' -c "select active, coalesce(wal_status, 'unbekannt'),
        coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)::bigint
        from pg_replication_slots where slot_name = '$PITR_SLOT'"
}

# Kette und Slot für den Empfänger bereitstellen; beginnt eine neue Kette, wenn die
# alte nicht lückenlos weitergeht. Gelöscht wird dabei nichts außer dem Slot.
pitr_prepare_chain() {
    local ident state chain name reason=""
    ident=$(pitr_psql -F ' ' -c "select (select system_identifier from pg_control_system()),
        (select setting from pg_settings where name = 'wal_segment_size')") || return 1
    [ -n "$ident" ] || return 1
    state=$(pitr_slot_state) || return 1
    chain=$(pitr_chain)
    if [ -z "$chain" ] || [ ! -d "$PITR_WAL_DIR/$chain" ]; then
        reason="noch keine Kette vorhanden"
    elif [ -e "$PITR_WAL_DIR/.neue-kette" ]; then
        reason="angefordert mit pitr-neue-kette"
    elif [ "$(cat "$PITR_WAL_DIR/$chain/.kennung" 2>/dev/null)" != "$ident" ]; then
        reason="anderer Datenbank-Cluster"
    elif [ -z "$state" ] && compgen -G "$PITR_WAL_DIR/$chain/*.gz*" >/dev/null; then
        reason="der Replikations-Slot $PITR_SLOT fehlt"
    elif [[ "$state" == *" lost "* ]]; then
        reason="der Replikations-Slot $PITR_SLOT hat WAL verloren"
    fi
    if [ -n "$reason" ]; then
        if [ -n "$state" ]; then
            repl pg_receivewal --drop-slot --slot="$PITR_SLOT" --no-password || return 1
        fi
        name="k$(date -u +%Y%m%dT%H%M%SZ)"
        mkdir -p "$PITR_WAL_DIR/$name" && chmod 700 "$PITR_WAL_DIR/$name" || return 1
        echo "$ident" > "$PITR_WAL_DIR/$name/.kennung"
        echo "$name" > "$PITR_WAL_DIR/aktuell"
        rm -f "$PITR_WAL_DIR/.neue-kette"
        log "Neue WAL-Kette $name ($reason)"
        if [ -n "$chain" ]; then
            echo "$(now_utc) WAL-Kette $chain endet, neue Kette $name: $reason. Zeitpunkte zwischen dem Ende" \
                "der alten Kette und der nächsten Basissicherung sind nicht erreichbar." \
                > "$STATUS_DIR/pitr_kette_unterbrochen"
        fi
    fi
    repl pg_receivewal --create-slot --if-not-exists --slot="$PITR_SLOT" --no-password
}

# Dauerprozess: hält pg_receivewal am Laufen. Der Slot lässt serverseitig nur einen
# Empfänger zu; die Sperre verhindert zusätzlich einen zweiten in diesem Container.
cmd_pitr_receive() {
    pitr_require_enabled
    local child="" stopping=0 delay=5 chain started rc requested=""
    exec 8>"$PITR_RECEIVE_LOCK_FILE"
    if ! flock -n 8; then
        log "In diesem Container läuft bereits ein WAL-Empfänger – Abbruch."
        exit 75
    fi
    mkdir -p "$PITR_WAL_DIR" && chmod 700 "$PITR_WAL_DIR" || exit 1
    trap 'stopping=1; if [ -n "$child" ]; then kill -INT "$child" 2>/dev/null; fi' TERM INT
    log "=== WAL-Empfang startet (Slot $PITR_SLOT, Rolle $PITR_REPL_USER)"
    while [ "$stopping" -eq 0 ]; do
        if pitr_prepare_chain; then
            chain=$(pitr_chain)
            started=$(date +%s)
            # --synchronous: Empfangenes sofort auf die Platte, auch im angefangenen Segment
            PGUSER="$PITR_REPL_USER" PGPASSWORD="${BACKUP_PITR_REPL_PASSWORD:-}" pg_receivewal \
                --directory="$PITR_WAL_DIR/$chain" --slot="$PITR_SLOT" --compress=6 --synchronous \
                --no-loop --no-password --status-interval=10 8>&- &
            child=$!
            if [ ! -e "$PITR_WAL_DIR/$chain/.basis" ] && [ "$requested" != "$chain" ]; then
                requested="$chain"
                log "Die WAL-Kette $chain hat noch keine Basissicherung – sie entsteht jetzt"
                (sleep 5; exec "$0" pitr-basis) 8>&- &
            fi
            rc=0
            wait "$child" || rc=$?
            # Nach einem Signal kehrt wait sofort zurück: auf das saubere Ende warten
            while kill -0 "$child" 2>/dev/null; do
                rc=0
                wait "$child" || rc=$?
            done
            child=""
            if [ "$stopping" -eq 1 ]; then break; fi
            if [ $(( $(date +%s) - started )) -ge 60 ]; then delay=5; fi
            log "WAL-Empfang endete mit Code $rc – neuer Versuch in $delay s"
        else
            log "WAL-Empfang: Datenbank, Rolle oder Slot nicht bereit – neuer Versuch in $delay s"
        fi
        sleep "$delay" &
        wait $! || true
        if [ "$delay" -lt 30 ]; then delay=$((delay * 2 > 30 ? 30 : delay * 2)); fi
    done
    log "=== WAL-Empfang beendet"
}

cmd_pitr_new_chain() {
    pitr_require_enabled
    mkdir -p "$PITR_WAL_DIR" && : > "$PITR_WAL_DIR/.neue-kette" || exit 1
    if pkill -INT -x pg_receivewal; then
        log "Der WAL-Empfänger beginnt gleich eine neue Kette; danach entsteht eine Basissicherung."
    else
        log "Kein laufender Empfänger gefunden; die neue Kette beginnt mit seinem nächsten Start."
    fi
}

# Zustand von Empfänger und Slot in die Statusdateien schreiben (für den Watchdog).
# Mehrere Versuche, damit ein Neustart der Datenbank keinen Fehlalarm auslöst.
pitr_check_receiver() {
    local attempt state active status lag problem=""
    for attempt in 1 2 3 4; do
        problem=""
        active="" status="" lag=0
        if ! state=$(pitr_slot_state 2>/dev/null); then
            problem="Die Datenbank $PGHOST ist nicht erreichbar, der WAL-Empfang steht"
        elif [ -z "$state" ]; then
            problem="Der Replikations-Slot $PITR_SLOT fehlt, der WAL-Empfang steht"
        else
            read -r active status lag <<< "$state"
            if [ "$active" != t ]; then
                problem="Der WAL-Empfang steht: Am Slot $PITR_SLOT hängt kein Empfänger, $((lag / 1048576)) MB WAL warten"
            elif [ "$status" = lost ] || [ "$status" = unreserved ]; then
                problem="Der Slot $PITR_SLOT verliert WAL (wal_status $status)"
            elif [ $((lag / 1048576)) -ge "$PITR_SLOT_WARN_MB" ]; then
                problem="Der Slot $PITR_SLOT staut $((lag / 1048576)) MB WAL (Warnschwelle $PITR_SLOT_WARN_MB MB)"
            fi
        fi
        if [ -z "$problem" ] || [ "$attempt" -eq 4 ]; then break; fi
        sleep "$PITR_RECHECK_SECONDS"
    done
    if [ -n "$problem" ]; then
        echo "$(now_utc) $problem" > "$STATUS_DIR/pitr_empfang_failed"
        log "FEHLER: $problem"
        return 1
    fi
    echo "$(now_utc) Slot $PITR_SLOT aktiv, Rückstand $((lag / 1048576)) MB, Kette $(pitr_chain)" \
        > "$STATUS_DIR/pitr_empfang_ok"
    rm -f "$STATUS_DIR/pitr_empfang_failed"
}

# WAL-Archiv in alle Repositorys übertragen. Der Aufrufer hält die Sperre.
pitr_ship_wal() {
    local chain dir seg waited=0 n rlabel failed=()
    chain=$(pitr_chain)
    dir="$PITR_WAL_DIR/$chain"
    if [ -z "$chain" ] || [ ! -d "$dir" ]; then
        echo "$(now_utc) Keine WAL-Kette unter $PITR_WAL_DIR – läuft der WAL-Empfang?" \
            > "$STATUS_DIR/pitr_wal_last_error"
        log "FEHLER: keine WAL-Kette unter $PITR_WAL_DIR"
        return 1
    fi
    # Angefangenes Segment abschließen, damit außer Haus höchstens der Takt fehlt.
    # Ohne Schreibzugriffe seit dem letzten Wechsel tut pg_switch_wal nichts.
    seg=$(pitr_psql -c "select pg_walfile_name(pg_switch_wal())" 2>/dev/null)
    while [ -n "$seg" ] && [ -e "$dir/$seg.gz.partial" ] && [ ! -e "$dir/$seg.gz" ] && [ "$waited" -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
    done
    for n in $(repo_numbers); do
        rlabel=$(repo_label "$n")
        if repo "$n" backup "$PITR_WAL_DIR" --host "$PITR_HOST_TAG" --tag pitr-wal --quiet \
            --exclude '*.partial' --exclude "$PITR_WAL_DIR/.neue-kette"; then
            log "[$rlabel] WAL übertragen (Kette $chain)"
        else
            failed+=("$rlabel")
        fi
    done
    if [ ${#failed[@]} -gt 0 ]; then
        echo "$(now_utc) WAL-Übertragung fehlgeschlagen: ${failed[*]}" > "$STATUS_DIR/pitr_wal_last_error"
        log "FEHLER: WAL-Übertragung ${failed[*]}"
        return 1
    fi
    echo "$(now_utc) WAL der Kette $chain in allen Repositorys" > "$STATUS_DIR/pitr_wal_last_success"
    rm -f "$STATUS_DIR/pitr_wal_last_error"
}

# Läuft im kurzen Takt. Ein geplanter Lauf hat Vorrang: Hält er die Sperre, entfällt
# diese Übertragung; er überträgt das WAL am Ende selbst (mit der Basissicherung).
cmd_pitr_wal() {
    pitr_require_enabled
    local rc=0
    pitr_check_receiver || rc=1
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "Ein anderer Backup-Vorgang läuft – das WAL folgt beim nächsten Takt."
        return "$rc"
    fi
    release_stale_locks
    pitr_ship_wal || rc=1
    return "$rc"
}

pitr_base_error() {
    echo "$(now_utc) $*" > "$STATUS_DIR/pitr_basis_last_error"
    log "FEHLER: $*"
}

# Physische Basissicherung in alle Repositorys, danach WAL übertragen und aufräumen.
# Der Aufrufer hält die Sperre.
pitr_base_backup() {
    local chain state label stage startseg ended n rlabel packing=gz started waited=0 stored=0 failed=()
    started=$(date +%s)
    log "=== Basissicherung startet (Stand $PITR_HOST_TAG)"
    case "$PITR_COMPRESS" in
        0) packing=tar ;;
        [1-9]) ;;
        *)
            pitr_base_error "BACKUP_PITR_COMPRESS muss zwischen 0 und 9 liegen"
            return 1
            ;;
    esac
    # Der Empfänger muss laufen, sonst fehlt das WAL nach dem Ende der Basissicherung
    while :; do
        chain=$(pitr_chain)
        state=$(pitr_slot_state 2>/dev/null)
        if [ -n "$chain" ] && [ "${state%% *}" = t ]; then break; fi
        if [ "$waited" -ge 60 ]; then
            pitr_base_error "Der WAL-Empfang läuft nicht (Slot $PITR_SLOT ohne Empfänger) – keine Basissicherung"
            return 1
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if [ "$(pitr_psql -c "select count(*) from pg_tablespace where spcname not in ('pg_default', 'pg_global')")" != 0 ]; then
        pitr_base_error "Zusätzliche Tablespaces werden nicht unterstützt"
        return 1
    fi

    label=$(date -u +%Y%m%dT%H%M%SZ)
    stage="$PITR_BASE_DIR/$label"
    if ! { rm -rf "$PITR_BASE_DIR" && mkdir -p "$stage" && chmod 700 "$PITR_BASE_DIR"; }; then
        pitr_base_error "Zwischenablage $PITR_BASE_DIR nicht anlegbar"
        return 1
    fi
    # Das Segment vor dem Start ist die sichere Untergrenze für das nötige WAL: Die
    # Sicherung beginnt mit einem Checkpoint, der erst nach dieser Abfrage anfängt.
    startseg=$(pitr_psql -c "select pg_walfile_name(pg_current_wal_lsn())")
    if [ -z "$startseg" ]; then
        pitr_base_error "WAL-Position der Datenbank nicht lesbar"
        return 1
    fi
    log "  pg_basebackup (Kette $chain, WAL ab Segment $startseg)"
    if ! repl pg_basebackup --pgdata="$stage" --format=tar --wal-method=stream --compress="$PITR_COMPRESS" \
        --checkpoint="$PITR_CHECKPOINT" --label="mandari-backup $label" --no-password; then
        rm -rf "$PITR_BASE_DIR"
        pitr_base_error "pg_basebackup fehlgeschlagen"
        return 1
    fi
    ended=$(date +%s)
    if [ "$(pitr_chain)" != "$chain" ]; then
        rm -rf "$PITR_BASE_DIR"
        pitr_base_error "Die WAL-Kette wechselte während der Basissicherung – sie wird verworfen"
        return 1
    fi
    du -sh "$stage"/* | sed 's/^/  /'

    for n in $(repo_numbers); do
        rlabel=$(repo_label "$n")
        log "[$rlabel] Basissicherung $label"
        if repo "$n" backup "$stage" --host "$PITR_HOST_TAG" \
            --tag "pitr-basis,kette=$chain,wal=$startseg,ende=$ended,packung=$packing"; then
            stored=$((stored + 1))
        else
            failed+=("$rlabel (Basissicherung)")
        fi
    done
    rm -rf "$PITR_BASE_DIR"
    if [ "$stored" -gt 0 ]; then : > "$PITR_WAL_DIR/$chain/.basis"; fi
    pitr_ship_wal || failed+=("WAL-Übertragung")
    pitr_retention || failed+=("Aufräumen")

    if [ ${#failed[@]} -gt 0 ]; then
        pitr_base_error "Basissicherung $label unvollständig: ${failed[*]}"
        return 1
    fi
    echo "$(now_utc) Basissicherung $label, Kette $chain, Dauer $(( $(date +%s) - started )) s" \
        > "$STATUS_DIR/pitr_basis_last_success"
    rm -f "$STATUS_DIR/pitr_basis_last_error"
    log "=== Basissicherung erfolgreich nach $(( $(date +%s) - started )) s"
}

cmd_pitr_basis() {
    pitr_require_enabled
    take_lock
    pitr_base_backup
}

# Basissicherungen eines Repositorys, je Zeile: Kennung, Ende (Sekunden seit 1970),
# Kette, erstes nötiges WAL-Segment, Packung, Pfad im Stand
pitr_base_list() {
    local json
    json=$(repo "$1" --retry-lock 10m snapshots --host "$PITR_HOST_TAG" --tag pitr-basis --json) || return 1
    jq -r '(. // [])[] | . as $s | ($s.tags // []) as $t
        | [ $s.id,
            ([$t[] | select(startswith("ende=")) | ltrimstr("ende=")][0] // ""),
            ([$t[] | select(startswith("kette=")) | ltrimstr("kette=")][0] // ""),
            ([$t[] | select(startswith("wal=")) | ltrimstr("wal=")][0] // ""),
            ([$t[] | select(startswith("packung=")) | ltrimstr("packung=")][0] // "gz"),
            ($s.paths[0] // "") ]
        | select((.[1] | test("^[0-9]+$")) and .[2] != "" and .[3] != "" and .[5] != "")
        | join(" ")' <<< "$json"
}

# Aufbewahrung als reine Rechnung: liest die Zeilen von pitr_base_list und stellt
# jeder das Urteil voran. Behalten wird, was im Fenster fertig wurde, dazu als Anker
# die jüngste Basissicherung davor: Von ihr aus ist der Anfang des Fensters erreichbar.
pitr_plan() {
    local window="$1" line id ended anchor="" anchor_end=-1 lines=()
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        lines+=("$line")
        read -r id ended _ <<< "$line"
        if [ "$ended" -lt "$window" ] && [ "$ended" -gt "$anchor_end" ]; then
            anchor="$id"
            anchor_end="$ended"
        fi
    done
    [ ${#lines[@]} -gt 0 ] || return 0
    for line in "${lines[@]}"; do
        read -r id ended _ <<< "$line"
        if [ "$ended" -ge "$window" ]; then
            echo "behalten $line"
        elif [ "$id" = "$anchor" ]; then
            echo "anker $line"
        else
            echo "loeschen $line"
        fi
    done
}

# Lokales WAL entfernen, das keine behaltene Basissicherung mehr braucht:
# ältere Ketten ganz, in der Kette $1 die Segmente vor $2.
pitr_prune_local_wal() {
    local keep_chain="$1" keep_seg="$2" current dir file name removed=0
    current=$(pitr_chain)
    for dir in "$PITR_WAL_DIR"/k*/; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        if [[ "$name" < "$keep_chain" ]] && [ "$name" != "$current" ]; then
            log "  WAL-Kette $name wird nicht mehr gebraucht – entfernt"
            rm -rf "$dir"
        fi
    done
    for file in "$PITR_WAL_DIR/$keep_chain"/*.gz; do
        [ -e "$file" ] || continue
        name=$(basename "$file" .gz)
        # Nur Segmente (24 Zeichen); Timeline-Dateien bleiben
        [[ "$name" =~ ^[0-9A-F]{24}$ ]] || continue
        if [[ "$name" < "$keep_seg" ]]; then
            rm -f "$file"
            removed=$((removed + 1))
        fi
    done
    log "  Lokales WAL: $removed Segmente vor $keep_seg (Kette $keep_chain) entfernt"
}

# Basissicherungen und WAL zusammen aufräumen. Lokales WAL wird nur gelöscht, wenn
# alle Repositorys lesbar waren, und nie etwas, das eine behaltene Basissicherung
# irgendeines Repositorys braucht. Den Platz gibt das Aufräumen der täglichen
# Sicherung frei (forget --prune im nächsten Lauf).
pitr_retention() {
    local window n rlabel list verdict id chain seg oldest="" complete=1 forget failed=()
    window=$(( $(date +%s) - PITR_KEEP_DAYS * 86400 ))
    for n in $(repo_numbers); do
        rlabel=$(repo_label "$n")
        if ! list=$(pitr_base_list "$n"); then
            complete=0
            failed+=("$rlabel (Liste)")
            continue
        fi
        forget=()
        while read -r verdict id _ chain seg _; do
            [ -n "$verdict" ] || continue
            if [ "$verdict" = loeschen ]; then
                forget+=("$id")
            elif [ -z "$oldest" ] || [[ "$chain $seg" < "$oldest" ]]; then
                oldest="$chain $seg"
            fi
        done <<< "$(pitr_plan "$window" <<< "$list")"
        if [ ${#forget[@]} -gt 0 ]; then
            log "[$rlabel] ${#forget[@]} Basissicherungen vor dem Fenster von $PITR_KEEP_DAYS Tagen entfernen"
            repo "$n" forget --quiet "${forget[@]}" || failed+=("$rlabel (Basissicherungen)")
        fi
        # Der neueste WAL-Stand enthält alles noch nötige WAL; ältere bleiben zwei Tage
        # als Schutz, falls das lokale Archiv beschädigt übertragen wurde
        repo "$n" forget --quiet --host "$PITR_HOST_TAG" --tag pitr-wal --keep-last 1 --keep-within-hourly 2d ||
            failed+=("$rlabel (WAL-Stände)")
    done
    if [ "$complete" -eq 1 ] && [ -n "$oldest" ]; then
        pitr_prune_local_wal "${oldest% *}" "${oldest#* }"
    else
        log "  Lokales WAL bleibt unverändert (keine Basissicherung oder Repository nicht lesbar)"
    fi
    if [ ${#failed[@]} -gt 0 ]; then
        log "FEHLER: Aufräumen ${failed[*]}"
        return 1
    fi
}

cmd_pitr_plan() {
    local now only="" n rlabel list window verdict id ended chain seg
    now=$(date +%s)
    while [ $# -gt 0 ]; do
        case "$1" in
            --jetzt)
                if [ $# -lt 2 ] || [[ ! "$2" =~ ^[0-9]+$ ]]; then
                    usage
                    exit 64
                fi
                now="$2"
                shift 2
                ;;
            *)
                only="$1"
                shift
                ;;
        esac
    done
    window=$(( now - PITR_KEEP_DAYS * 86400 ))
    log "Aufbewahrung $PITR_KEEP_DAYS Tage: erreichbar bleibt jeder Zeitpunkt ab $(pitr_utc "$window")"
    for n in $(repo_numbers); do
        if [ -n "$only" ] && [ "$only" != "$n" ]; then continue; fi
        rlabel=$(repo_label "$n")
        log "=== $rlabel"
        list=$(pitr_base_list "$n") || return 1
        while read -r verdict id ended chain seg _; do
            [ -n "$verdict" ] || continue
            printf '  %-8s Basissicherung fertig %s, Kette %s, WAL ab %s (%s)\n' \
                "$verdict" "$(pitr_utc "$ended")" "$chain" "$seg" "${id:0:8}"
        done <<< "$(pitr_plan "$window" <<< "$list")"
    done
}

# Erste Lücke in der Folge vollständiger Segmente eines Verzeichnisses: gibt das
# Segment aus, nach dem eines fehlt (leer = lückenlos)
pitr_wal_gap() {
    local dir="$1" per_log file name seq prev=-1 prev_name=""
    per_log=$(( 4294967296 / $2 ))
    for file in "$dir"/*.gz; do
        [ -e "$file" ] || continue
        name=$(basename "$file" .gz)
        [[ "$name" =~ ^[0-9A-F]{24}$ ]] || continue
        seq=$(( 16#${name:8:8} * per_log + 16#${name:16:8} ))
        if [ "$prev" -ge 0 ] && [ "$seq" -ne $((prev + 1)) ]; then
            echo "$prev_name"
            return 0
        fi
        prev=$seq
        prev_name=$name
    done
}

cmd_pitr_status() {
    local chain state dir name files gap n rlabel list id ended kette seg latest
    chain=$(pitr_chain)
    if pitr_enabled; then
        log "PITR eingeschaltet, Fenster $PITR_KEEP_DAYS Tage"
    else
        log "PITR ausgeschaltet (BACKUP_PITR)"
    fi
    if state=$(pitr_slot_state 2>/dev/null); then
        log "Slot $PITR_SLOT (aktiv, wal_status, Rückstand in Bytes): ${state:-fehlt}"
    else
        log "Slot $PITR_SLOT: Datenbank $PGHOST nicht erreichbar"
    fi
    for dir in "$PITR_WAL_DIR"/k*/; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        files=("$dir"*.gz)
        if [ ! -e "${files[0]}" ]; then
            log "Kette $name: noch kein vollständiges Segment"
            continue
        fi
        gap=$(pitr_wal_gap "${dir%/}" "$(pitr_segsize "${dir%/}")")
        if [ "$name" = "$chain" ]; then name="$name (läuft)"; fi
        log "Kette $name: ${#files[@]} Dateien, $(basename "${files[0]}" .gz) bis $(basename "${files[-1]}" .gz)," \
            "Lücke: ${gap:+nach }${gap:-keine}"
    done
    for n in $(repo_numbers); do
        rlabel=$(repo_label "$n")
        log "=== $rlabel"
        list=$(pitr_base_list "$n") || return 1
        while read -r id ended kette seg _; do
            [ -n "$id" ] || continue
            printf '  Basissicherung fertig %s, Kette %s, WAL ab %s (%s)\n' \
                "$(pitr_utc "$ended")" "$kette" "$seg" "${id:0:8}"
        done <<< "$list"
        latest=$(repo "$n" snapshots --host "$PITR_HOST_TAG" --tag pitr-wal --latest 1 --json |
            jq -r '(. // []) | (.[-1].time // "keiner")')
        echo "  Neuester WAL-Stand: $latest"
    done
}

# Zeitangabe "JJJJ-MM-TT HH:MM[:SS]" in Sekunden seit 1970 umrechnen. Ohne Zusatz gilt
# die Zeitzone des Containers (TZ aus BACKUP_TZ), mit Z oder ±HH[:MM] der Zusatz.
pitr_parse_time() {
    local re='^([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]([0-9]{2}:[0-9]{2})(:[0-9]{2})?(Z|[+-][0-9]{2}(:?[0-9]{2})?)?$'
    local base zone digits mins epoch back offset
    [[ "$1" =~ $re ]] || return 1
    base="${BASH_REMATCH[1]} ${BASH_REMATCH[2]}${BASH_REMATCH[3]:-:00}"
    zone="${BASH_REMATCH[4]}"
    if [ -z "$zone" ]; then
        epoch=$(date -d "$base" +%s 2>/dev/null) || return 1
        back=$(date -d "@$epoch" '+%Y-%m-%d %H:%M:%S')
    else
        epoch=$(date -u -d "$base" +%s 2>/dev/null) || return 1
        back=$(date -u -d "@$epoch" '+%Y-%m-%d %H:%M:%S')
    fi
    # Fängt Angaben ab, die es nicht gibt (31. Februar, übersprungene Stunde der Sommerzeit)
    [ "$back" = "$base" ] || return 1
    if [ -n "$zone" ] && [ "$zone" != Z ]; then
        digits="${zone:1}"
        digits="${digits/:/}"
        mins="${digits:2:2}"
        offset=$(( 10#${digits:0:2} * 3600 + 10#${mins:-0} * 60 ))
        if [ "${zone:0:1}" = + ]; then epoch=$((epoch - offset)); else epoch=$((epoch + offset)); fi
    fi
    echo "$epoch"
}

# Jüngste Basissicherung des Repositorys $1, die vor dem Zeitpunkt $2 fertig war
# (leer = die neueste überhaupt); Ausgabe wie pitr_base_list
pitr_pick_base() {
    local target="$2" list line id ended best="" best_end=-1
    list=$(pitr_base_list "$1") || return 1
    while IFS= read -r line; do
        read -r id ended _ <<< "$line"
        [ -n "$id" ] || continue
        if [ -n "$target" ] && [ "$ended" -gt "$target" ]; then continue; fi
        if [ "$ended" -gt "$best_end" ]; then
            best="$line"
            best_end="$ended"
        fi
    done <<< "$list"
    [ -n "$best" ] || return 1
    echo "$best"
}

pitr_cleanup() {
    if [ -f "$PITR_RESTORE_DIR/pgdata/postmaster.pid" ]; then
        su-exec postgres pg_ctl -D "$PITR_RESTORE_DIR/pgdata" -m fast -w stop >/dev/null 2>&1
    fi
    rm -rf "$PITR_RESTORE_DIR"
}

# WAL für die Wiederherstellung bereitstellen: ab dem Segment $2 aus dem Verzeichnis $1
# ($3 = cp oder mv). Ein angefangenes Segment des Empfängers wird entpackt und auf die
# Segmentgröße aufgefüllt; PostgreSQL liest es bis zum letzten vollständigen Eintrag.
pitr_stage_wal() {
    local src="$1" first="$2" how="$3" file name partial="" count=0
    for file in "$src"/*; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        case "$name" in
            *.history | *.history.gz) ;;
            *.gz.partial)
                partial="$file"
                continue
                ;;
            *.gz) if [[ "${name%.gz}" < "$first" ]]; then continue; fi ;;
            *) continue ;;
        esac
        "$how" "$file" "$PITR_RESTORE_DIR/wal/$name" || return 1
        count=$((count + 1))
    done
    if [ -n "$partial" ]; then
        name=$(basename "$partial" .gz.partial)
        if [ ! -e "$PITR_RESTORE_DIR/wal/$name.gz" ] && ! [[ "$name" < "$first" ]]; then
            # Endet erwartbar mit "unexpected end of file": Der Strom ist nicht abgeschlossen
            gzip -dc "$partial" 2>/dev/null > "$PITR_RESTORE_DIR/wal/$name"
            if [ -s "$PITR_RESTORE_DIR/wal/$name" ]; then
                truncate -s "$(pitr_segsize "$src")" "$PITR_RESTORE_DIR/wal/$name"
                log "  Angefangenes Segment $name des Empfängers einbezogen"
                count=$((count + 1))
            else
                rm -f "$PITR_RESTORE_DIR/wal/$name"
            fi
        fi
    fi
    log "  $count WAL-Dateien ab Segment $first bereitgestellt"
}

# Die Wegwerf-Instanz übernimmt die postgresql.conf der Sicherung. Überschrieben wird,
# was sie von außen erreichbar machte oder im Container fehlt (TLS-Dateien, geladene
# Bibliotheken). Ohne hot_standby verlangt PostgreSQL keine Verbindungs- und
# Sperrgrenzen in Höhe der Quelle; Meldungen englisch und in UTC, weil das Skript sie liest.
pitr_start_instance() {
    local options="-p $PITR_PORT -c listen_addresses= -c unix_socket_directories=$PITR_RESTORE_DIR/run"
    options="$options -c hba_file=$PITR_RESTORE_DIR/pg_hba.conf -c hot_standby=off -c archive_mode=off"
    options="$options -c shared_preload_libraries= -c ssl=off -c logging_collector=off -c log_destination=stderr"
    options="$options -c lc_messages=C -c timezone=UTC -c log_timezone=UTC -c fsync=off"
    # Ohne die Sperrdateien dieses Skripts: Die Instanz kann es überleben (--behalten)
    su-exec postgres pg_ctl -D "$PITR_RESTORE_DIR/pgdata" -l "$PITR_RESTORE_DIR/postgres.log" -W \
        -o "$options" start >/dev/null 7>&- 9>&-
}

# Wartet, bis die Wegwerf-Instanz Verbindungen annimmt (0), sich beendet hat (1)
# oder die Zeit abgelaufen ist (2). Während der Wiederherstellung lehnt sie ab.
pitr_wait_instance() {
    local waited=0 gone=0
    while [ "$waited" -lt "$PITR_RESTORE_TIMEOUT" ]; do
        if pg_isready -q "${PITR_P[@]}"; then return 0; fi
        if su-exec postgres pg_ctl -D "$PITR_RESTORE_DIR/pgdata" status >/dev/null 2>&1; then
            gone=0
        else
            gone=$((gone + 1))
            if [ "$gone" -ge 5 ]; then return 1; fi
        fi
        sleep 1
        waited=$((waited + 1))
        if [ $((waited % 60)) -eq 0 ]; then log "  WAL wird eingespielt … ($waited s)"; fi
    done
    return 2
}

pitr_restore_build() {
    local n="$1" target="$2" source="$3" base id ended chain seg packing path ext=tar.gz src gap rc
    local untar=(tar -x -f -)
    if ! base=$(pitr_pick_base "$n" "$target"); then
        PITR_ERROR="Im Repository $(repo_label "$n") gibt es keine Basissicherung, die vor dem Zeitpunkt fertig war"
        return 1
    fi
    read -r id ended chain seg packing path <<< "$base"
    if [ "$packing" = tar ]; then ext=tar; else untar+=(-z); fi
    PITR_BASE="$(basename "$path") (fertig $(pitr_utc "$ended"), Kette $chain)"
    log "  Basissicherung $PITR_BASE"

    rm -rf "$PITR_RESTORE_DIR"
    mkdir -p "$PITR_RESTORE_DIR/pgdata" "$PITR_RESTORE_DIR/run" "$PITR_RESTORE_DIR/wal" || return 1
    chmod 700 "$PITR_RESTORE_DIR/pgdata"
    if ! repo "$n" --retry-lock 10m dump "$id" "$path/base.$ext" | "${untar[@]}" -C "$PITR_RESTORE_DIR/pgdata"; then
        PITR_ERROR="Die Basissicherung ließ sich nicht aus dem Repository holen oder entpacken"
        return 1
    fi
    if ! repo "$n" --retry-lock 10m dump "$id" "$path/pg_wal.$ext" |
        "${untar[@]}" -C "$PITR_RESTORE_DIR/pgdata/pg_wal"; then
        PITR_ERROR="Das WAL der Basissicherung ließ sich nicht holen oder entpacken"
        return 1
    fi

    if [ "$source" = auto ]; then
        if [ -d "$PITR_WAL_DIR/$chain" ]; then source=lokal; else source=ablage; fi
    fi
    if [ "$source" = lokal ]; then
        src="$PITR_WAL_DIR/$chain"
        if [ ! -d "$src" ]; then
            PITR_ERROR="Die WAL-Kette $chain liegt nicht im lokalen Archiv (--wal ablage holt sie aus dem Repository)"
            return 1
        fi
        log "  WAL aus dem lokalen Archiv $src"
        pitr_stage_wal "$src" "$seg" cp || return 1
    else
        log "  WAL aus dem neuesten WAL-Stand des Repositorys $(repo_label "$n")"
        if ! repo "$n" --retry-lock 10m restore latest --host "$PITR_HOST_TAG" --tag pitr-wal \
            --target "$PITR_RESTORE_DIR/ablage" --include "$PITR_WAL_DIR/$chain" >/dev/null; then
            PITR_ERROR="Das WAL-Archiv ließ sich nicht aus dem Repository holen"
            return 1
        fi
        src="$PITR_RESTORE_DIR/ablage$PITR_WAL_DIR/$chain"
        if [ ! -d "$src" ]; then
            PITR_ERROR="Die WAL-Kette $chain fehlt im neuesten WAL-Stand des Repositorys"
            return 1
        fi
        pitr_stage_wal "$src" "$seg" mv || return 1
    fi
    gap=$(pitr_wal_gap "$PITR_RESTORE_DIR/wal" "$(pitr_segsize "$src")")
    rm -rf "$PITR_RESTORE_DIR/ablage"
    if [ -n "$gap" ]; then
        log "  ACHTUNG: Im WAL fehlt das Segment nach $gap; spätere Zeitpunkte sind nicht erreichbar"
    fi

    cat > "$PITR_RESTORE_DIR/wal-holen.sh" <<EOF
#!/bin/sh
# restore_command der Wegwerf-Instanz: Segment \$1 aus dem bereitgestellten WAL nach \$2
if [ -f "$PITR_RESTORE_DIR/wal/\$1.gz" ]; then
    exec gzip -dc "$PITR_RESTORE_DIR/wal/\$1.gz" > "\$2"
fi
if [ -f "$PITR_RESTORE_DIR/wal/\$1" ]; then
    exec cp "$PITR_RESTORE_DIR/wal/\$1" "\$2"
fi
exit 1
EOF
    chmod 755 "$PITR_RESTORE_DIR/wal-holen.sh"
    echo "local all all trust" > "$PITR_RESTORE_DIR/pg_hba.conf"
    {
        echo "# mandari-backup pitr"
        echo "restore_command = '$PITR_RESTORE_DIR/wal-holen.sh \"%f\" \"%p\"'"
        echo "recovery_target_action = 'promote'"
        if [ -n "$target" ]; then
            echo "recovery_target_time = '$(date -u -d "@$target" '+%Y-%m-%d %H:%M:%S')+00'"
        fi
    } >> "$PITR_RESTORE_DIR/pgdata/postgresql.auto.conf"
    rm -f "$PITR_RESTORE_DIR/pgdata/standby.signal"
    : > "$PITR_RESTORE_DIR/pgdata/recovery.signal"
    # Die Wegwerf-Instanz läuft als Benutzer postgres: Arbeitsverzeichnis nur durchquerbar machen
    chmod 711 "$WORK_DIR"
    chown -R postgres:postgres "$PITR_RESTORE_DIR"

    log "  Wegwerf-Instanz startet und spielt das WAL ein"
    pitr_start_instance
    rc=0
    pitr_wait_instance || rc=$?
    if [ -n "$target" ]; then
        if [ "$rc" -eq 0 ] && grep -q "recovery stopping" "$PITR_RESTORE_DIR/postgres.log"; then
            PITR_REACHED=ja
        elif [ "$rc" -eq 0 ]; then
            PITR_REACHED=nein
        elif [ "$rc" -eq 1 ] && grep -q "recovery ended before configured recovery target was reached" \
            "$PITR_RESTORE_DIR/postgres.log"; then
            # PostgreSQL bricht ab, wenn das WAL vor dem Ziel endet. Dann bis zum Ende des
            # Archivs einspielen und das Ergebnis deutlich als unvollständig ausweisen.
            log "  Das WAL-Archiv endet vor dem Zeitpunkt – es wird bis zu seinem Ende eingespielt"
            sed -i '/^recovery_target_time/d' "$PITR_RESTORE_DIR/pgdata/postgresql.auto.conf"
            PITR_REACHED=nein
            pitr_start_instance
            rc=0
            pitr_wait_instance || rc=$?
        fi
    fi
    if [ "$rc" -ne 0 ]; then
        tail -n 25 "$PITR_RESTORE_DIR/postgres.log" 2>/dev/null | sed 's/^/    /'
        if [ "$rc" -eq 2 ]; then
            PITR_ERROR="Die Wiederherstellung dauerte länger als $PITR_RESTORE_TIMEOUT s"
        else
            PITR_ERROR="Die Wegwerf-Instanz startete nicht (Protokoll oben)"
        fi
        return 1
    fi
    PITR_LAST_XACT=$(psql "${PITR_P[@]}" -d postgres -Atc "select coalesce(to_char(
        pg_last_xact_replay_timestamp() at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '')")
    if [ -z "$PITR_LAST_XACT" ]; then
        PITR_LAST_XACT=$(sed -n 's/.*last completed transaction was at log time //p' \
            "$PITR_RESTORE_DIR/postgres.log" | tail -n 1)
    fi
}

# Stand zum Zeitpunkt $2 (Sekunden seit 1970, leer = Ende des Archivs) aus dem
# Repository $1 in einer Wegwerf-Instanz unter $PITR_RESTORE_DIR aufbauen; $3 nennt die
# Quelle des WAL (auto, lokal, ablage). Ergebnis: PITR_REACHED (ja, nein, ende),
# PITR_LAST_XACT, PITR_BASE; im Fehlerfall PITR_ERROR. Die laufende Datenbank wird
# nicht berührt.
pitr_restore() {
    PITR_ERROR="" PITR_REACHED=ende PITR_LAST_XACT="" PITR_BASE=""
    if [ -z "$(repo_url "$1")" ]; then
        PITR_ERROR="Das Repository $1 ist nicht eingerichtet"
        return 1
    fi
    exec 7>"$PITR_LOCK_FILE"
    if ! flock -n 7; then
        PITR_ERROR="Es läuft bereits eine Wiederherstellung auf einen Zeitpunkt"
        return 1
    fi
    if [ -e "$PITR_RESTORE_DIR/pgdata/postmaster.pid" ]; then
        PITR_ERROR="Eine Wegwerf-Instanz läuft noch – erst mandari-backup pitr-aufraeumen"
        return 1
    fi
    if ! pitr_restore_build "$@"; then
        pitr_cleanup
        return 1
    fi
}

# Zeilenzahlen der größten Tabellen der wiederhergestellten Datenbank $1
pitr_report_counts() {
    local db="$1" tables table
    if [ "$(psql "${PITR_P[@]}" -d postgres -Atc "select count(*) from pg_database where datname = '$db'")" != 1 ]; then
        log "  Die Datenbank $db gibt es im wiederhergestellten Stand nicht. Vorhanden:" \
            "$(psql "${PITR_P[@]}" -d postgres -Atc "select string_agg(datname, ', ' order by datname)
                from pg_database where not datistemplate")"
        return 0
    fi
    # Nach einer Wiederherstellung sind die Laufzeitstatistiken leer: Größe aus dem Katalog
    tables=$(psql "${PITR_P[@]}" -d "$db" -Atc "select format('%I.%I', n.nspname, c.relname)
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
        order by c.reltuples desc, 1 limit 8")
    log "  Zeilenzahlen der größten Tabellen in $db"
    while IFS= read -r table; do
        [ -n "$table" ] || continue
        printf '    %-50s %12s\n' "$table" "$(psql "${PITR_P[@]}" -d "$db" -Atc "select count(*) from $table")"
    done <<< "$tables"
}

cmd_pitr() {
    local when="" n=1 keep=0 source=auto db="$RESTORE_TEST_DB" target="" started
    while [ $# -gt 0 ]; do
        case "$1" in
            --zeitpunkt=*)
                when="${1#*=}"
                shift
                ;;
            --zeitpunkt | --repo | --wal | --db)
                [ $# -ge 2 ] || { usage; exit 64; }
                case "$1" in
                    --zeitpunkt) when="$2" ;;
                    --repo) n="$2" ;;
                    --wal) source="$2" ;;
                    *) db="$2" ;;
                esac
                shift 2
                ;;
            --behalten)
                keep=1
                shift
                ;;
            *)
                usage
                exit 64
                ;;
        esac
    done
    if [ -z "$when" ] || [[ ! "$n" =~ ^[1-3]$ ]] || [[ ! "$source" =~ ^(auto|lokal|ablage)$ ]] ||
        [[ ! "$db" =~ ^[A-Za-z0-9_-]+$ ]]; then
        usage
        exit 64
    fi
    started=$(date +%s)
    if [ "$when" = ende ]; then
        log "=== Zurück auf das Ende des WAL-Archivs – Repository $(repo_label "$n")"
    else
        if ! target=$(pitr_parse_time "$when"); then
            log "FEHLER: Zeitpunkt \"$when\" nicht lesbar. Erwartet: JJJJ-MM-TT HH:MM[:SS] in der Zeitzone" \
                "${TZ:-UTC}, wahlweise mit Z oder ±HH:MM, oder \"ende\"."
            exit 64
        fi
        if [ "$target" -gt "$started" ]; then
            log "FEHLER: Der Zeitpunkt $(pitr_utc "$target") liegt in der Zukunft."
            exit 64
        fi
        log "=== Zurück auf $(date -d "@$target" '+%Y-%m-%d %H:%M:%S %Z') ($(pitr_utc "$target"))" \
            "– Repository $(repo_label "$n")"
    fi
    if ! pitr_restore "$n" "$target" "$source"; then
        log "FEHLER: $PITR_ERROR"
        exit 1
    fi

    case "$PITR_REACHED" in
        ja) log "  Zeitpunkt erreicht. Letzte eingespielte Transaktion: ${PITR_LAST_XACT:-unbekannt}" ;;
        ende) log "  Ende des WAL-Archivs erreicht. Letzte eingespielte Transaktion: ${PITR_LAST_XACT:-unbekannt}" ;;
        *)
            log "  ACHTUNG: Das WAL-Archiv endet vor dem Zeitpunkt. Letzte eingespielte Transaktion:" \
                "${PITR_LAST_XACT:-unbekannt}. Was danach geschah, fehlt in diesem Stand."
            ;;
    esac
    pitr_report_counts "$db"
    if [ "$keep" -eq 1 ]; then
        log "  Die Wegwerf-Instanz läuft weiter (nur in diesem Container erreichbar):"
        log "    psql -h $PITR_RESTORE_DIR/run -p $PITR_PORT -U $PGUSER $db"
        log "  Beenden und löschen: mandari-backup pitr-aufraeumen"
    else
        pitr_cleanup
    fi
    log "=== Wiederherstellung auf den Zeitpunkt fertig nach $(( $(date +%s) - started )) s"
    if [ "$PITR_REACHED" = nein ]; then exit 2; fi
}

# Zeitpunkt-Probe: zwei Markierungen in der laufenden Datenbank, dazwischen der
# Zielzeitpunkt. Basissicherung und WAL kommen aus dem Repository $1; im Ergebnis muss
# die erste Markierung da sein und die zweite fehlen. Der Aufrufer hält die Sperre.
pitr_probe() {
    local n="$1" chain dir id target seg waited=0 first second
    PITR_ERROR="" PITR_PROBE_RESULT=""
    log "  Zeitpunkt-Probe: zwei Markierungen, Wiederherstellung auf einen Zeitpunkt dazwischen"
    chain=$(pitr_chain)
    dir="$PITR_WAL_DIR/$chain"
    if [ -z "$chain" ] || [ ! -d "$dir" ]; then
        PITR_ERROR="keine WAL-Kette – läuft der WAL-Empfang?"
        return 1
    fi
    id="probe-$(date -u +%Y%m%dT%H%M%SZ)"
    if ! PGOPTIONS="-c client_min_messages=warning" pitr_psql -q \
        -c "create schema if not exists mandari_backup" \
        -c "create table if not exists mandari_backup.pitr_probe
            (marke text primary key, zeit timestamptz not null default clock_timestamp())" \
        -c "insert into mandari_backup.pitr_probe (marke) values ('$id-1')"; then
        PITR_ERROR="erste Markierung nicht schreibbar"
        return 1
    fi
    # Zwei Sekunden Abstand je Seite: Das Ziel ist auf ganze Sekunden abgerundet
    sleep 2
    target=$(pitr_psql -c "select floor(extract(epoch from clock_timestamp()))::bigint")
    sleep 2
    seg=$(pitr_psql -q -c "insert into mandari_backup.pitr_probe (marke) values ('$id-2')" \
        -c "select pg_walfile_name(pg_switch_wal())" | tail -n 1)
    if [ -z "$target" ] || [ -z "$seg" ]; then
        PITR_ERROR="zweite Markierung nicht schreibbar"
        return 1
    fi
    while [ ! -e "$dir/$seg.gz" ]; do
        if [ "$waited" -ge 120 ]; then
            PITR_ERROR="das Segment $seg mit der zweiten Markierung kam nicht im WAL-Archiv an"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if ! pitr_ship_wal; then
        PITR_ERROR="WAL-Übertragung fehlgeschlagen"
        return 1
    fi
    if ! pitr_restore "$n" "$target" ablage; then
        PITR_ERROR="Wiederherstellung auf $(pitr_utc "$target"): $PITR_ERROR"
        return 1
    fi
    first=$(psql "${PITR_P[@]}" -d postgres -Atc "select count(*) from mandari_backup.pitr_probe where marke = '$id-1'")
    second=$(psql "${PITR_P[@]}" -d postgres -Atc "select count(*) from mandari_backup.pitr_probe where marke = '$id-2'")
    pitr_cleanup
    pitr_psql -q -c "delete from mandari_backup.pitr_probe" >/dev/null
    if [ "$PITR_REACHED" != ja ] || [ "$first" != 1 ] || [ "$second" != 0 ]; then
        PITR_ERROR="Ziel $(pitr_utc "$target") erreicht: $PITR_REACHED; erste Markierung ${first:-?}-mal,"
        PITR_ERROR="$PITR_ERROR zweite ${second:-?}-mal vorhanden (erwartet 1 und 0)"
        return 1
    fi
    PITR_PROBE_RESULT="Zeitpunkt-Probe bestanden (Ziel $(pitr_utc "$target"): erste Markierung da, zweite nicht)"
    log "  $PITR_PROBE_RESULT"
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
    sed -n '4,25p' "$0" | sed 's/^# \{0,1\}//'
}

# Eingebunden statt aufgerufen (tests/pitr-einheiten.sh): nur die Funktionen bereitstellen
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi

mkdir -p "$STATUS_DIR" "$WORK_DIR"

case "${1:-}" in
    init) cmd_init ;;
    run) cmd_run ;;
    check) cmd_check ;;
    restore-test) shift; cmd_restore_test "$@" ;;
    snapshots) cmd_snapshots ;;
    pitr) shift; cmd_pitr "$@" ;;
    pitr-aufraeumen) pitr_cleanup ;;
    pitr-status) cmd_pitr_status ;;
    pitr-basis) cmd_pitr_basis ;;
    pitr-wal) cmd_pitr_wal ;;
    pitr-plan) shift; cmd_pitr_plan "$@" ;;
    pitr-neue-kette) cmd_pitr_new_chain ;;
    pitr-empfang) cmd_pitr_receive ;;
    restic)
        shift
        [ $# -ge 1 ] || { usage; exit 64; }
        number="$1"
        shift
        repo "$number" "$@"
        ;;
    *) usage; exit 64 ;;
esac
