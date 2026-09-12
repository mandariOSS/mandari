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

echo "mandari-backup: $(restic version | cut -d' ' -f1-2), $(pg_dump --version), Zeitzone ${TZ:-UTC}"
echo "Zeitpläne:"
sed 's/^/  /' /etc/crontabs/root

exec crond -f -l 6
