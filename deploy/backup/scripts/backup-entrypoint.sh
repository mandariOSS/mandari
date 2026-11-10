#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Entrypoint des Backup-Containers: Umgebung für Cron ablegen, Zeitpläne setzen,
# crond im Vordergrund starten. Mit Argumenten wird stattdessen der Befehl ausgeführt.
set -euo pipefail

# busybox-crond gibt die Container-Umgebung nicht an Jobs weiter
(
    umask 077
    export -p | grep -E '^declare -x (RESTIC_|BACKUP_|PG|TZ=)' > /run/mandari-backup.env
)

if [ $# -gt 0 ]; then
    exec "$@"
fi

cat > /etc/crontabs/root <<EOF
${BACKUP_CRON:-30 2 * * *} /usr/local/bin/mandari-backup run > /proc/1/fd/1 2>&1
${BACKUP_CHECK_CRON:-30 6 * * 0} /usr/local/bin/mandari-backup check > /proc/1/fd/1 2>&1
${BACKUP_RESTORE_TEST_CRON:-30 4 3 * *} /usr/local/bin/mandari-backup restore-test > /proc/1/fd/1 2>&1
EOF

pitr="${BACKUP_PITR:-0}"
if [ "$pitr" != 0 ] && [ "$pitr" != 1 ]; then
    echo "BACKUP_PITR=$pitr ist unbekannt (0 oder 1) – PITR bleibt aus" >&2
    pitr=0
fi
if [ "$pitr" = 1 ]; then
    if [ -z "${BACKUP_PITR_REPL_PASSWORD:-}" ]; then
        echo "BACKUP_PITR=1, aber BACKUP_PITR_REPL_PASSWORD fehlt: Der WAL-Empfang kann sich nicht anmelden" >&2
    fi
    # Die Übertragung des WAL außer Haus; den Empfang selbst hält pitr-empfang am Laufen
    echo "${BACKUP_PITR_WAL_CRON:-*/15 * * * *} /usr/local/bin/mandari-backup pitr-wal > /proc/1/fd/1 2>&1" \
        >> /etc/crontabs/root
fi

echo "mandari-backup: $(restic version | cut -d' ' -f1-2), $(pg_dump --version), Zeitzone ${TZ:-UTC}"
echo "Zeitpläne:"
sed 's/^/  /' /etc/crontabs/root

if [ "$pitr" != 1 ]; then
    exec crond -f -l 6
fi

# Mit PITR laufen zwei Dauerprozesse: crond und der WAL-Empfänger. Endet einer von
# beiden unerwartet, endet der Container und Docker startet ihn neu.
echo "PITR eingeschaltet: WAL-Empfang über den Slot ${BACKUP_PITR_SLOT:-mandari_pitr}"
crond -f -l 6 &
crond_pid=$!
/usr/local/bin/mandari-backup pitr-empfang &
empfang_pid=$!

stop() {
    trap - TERM INT
    kill -TERM "$empfang_pid" "$crond_pid" 2>/dev/null || true
    wait "$empfang_pid" 2>/dev/null || true
    exit "${1:-0}"
}
trap 'stop 0' TERM INT

rc=0
wait -n || rc=$?
echo "Ein Dauerprozess endete unerwartet (Code $rc) – der Container wird beendet" >&2
stop 1
