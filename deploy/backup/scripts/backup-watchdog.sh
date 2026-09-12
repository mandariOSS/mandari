#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# Überwachung der Backups (stündlich per Cron auf dem Host, als root)
# =============================================================================
# Meldet per Mail über den mandari-Container (SMTP aus den Systemeinstellungen):
#   - Backup-Container läuft nicht
#   - letzte Sicherung endete mit Fehler
#   - letzte erfolgreiche Sicherung älter als BACKUP_MAX_AGE_HOURS
#   - Integritätsprüfung oder Wiederherstellungstest fehlgeschlagen
# Solange ein Problem besteht, höchstens eine Mail je 6 Stunden.
#
#   scripts/backup-watchdog.sh          # prüfen (Exit 1 bei Problemen)
#   scripts/backup-watchdog.sh --test   # Testmail senden
# =============================================================================
set -uo pipefail

BACKUP_DIR="${BACKUP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

env_value() {
    grep -E "^$1=" "$BACKUP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/'
}

STATUS_DIR="$(env_value BACKUP_STATUS_DIR)"
STATUS_DIR="${STATUS_DIR:-./status}"
case "$STATUS_DIR" in
    /*) ;;
    *) STATUS_DIR="$BACKUP_DIR/${STATUS_DIR#./}" ;;
esac
MAX_AGE_HOURS="$(env_value BACKUP_MAX_AGE_HOURS)"; MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"
ALERT_TO="$(env_value BACKUP_ALERT_TO)"
MAIL_CONTAINER="$(env_value BACKUP_MAIL_CONTAINER)"; MAIL_CONTAINER="${MAIL_CONTAINER:-mandari}"
BACKUP_CONTAINER="$(env_value BACKUP_COMPOSE_PROJECT)"; BACKUP_CONTAINER="${BACKUP_CONTAINER:-mandari-backup}"
HOST_LABEL="$(env_value BACKUP_HOSTNAME)"; HOST_LABEL="${HOST_LABEL:-$(hostname)}"
ALERT_STAMP="$STATUS_DIR/.alert-sent"

mkdir -p "$STATUS_DIR"

send_mail() {
    local subject="$1" body="$2"
    if [ -z "$ALERT_TO" ]; then
        echo "BACKUP_ALERT_TO ist nicht gesetzt – keine Mail versendet" >&2
        return 1
    fi
    docker exec -e ALERT_SUBJECT="$subject" -e ALERT_BODY="$body" -e ALERT_TO="$ALERT_TO" "$MAIL_CONTAINER" \
        python manage.py shell -c "import os
from django.core.mail import send_mail
send_mail(os.environ['ALERT_SUBJECT'], os.environ['ALERT_BODY'], None,
          [a.strip() for a in os.environ['ALERT_TO'].split(',') if a.strip()])" >/dev/null 2>&1
}

if [ "${1:-}" = "--test" ]; then
    if send_mail "[$HOST_LABEL] Backup-Überwachung: Testmail" \
        "Die Backup-Überwachung auf $HOST_LABEL kann Mails versenden. Es ist keine Aktion nötig."; then
        echo "Testmail an $ALERT_TO versendet"
        exit 0
    fi
    echo "Testmail fehlgeschlagen" >&2
    exit 1
fi

now=$(date +%s)
problems=()

running=$(docker inspect -f '{{.State.Running}}' "$BACKUP_CONTAINER" 2>/dev/null || echo "nicht vorhanden")
[ "$running" = "true" ] || problems+=("Der Backup-Container $BACKUP_CONTAINER läuft nicht (Status: $running).")

if [ -f "$STATUS_DIR/last_success" ]; then
    age=$(( (now - $(stat -c %Y "$STATUS_DIR/last_success")) / 3600 ))
    if [ "$age" -ge "$MAX_AGE_HOURS" ]; then
        problems+=("Die letzte erfolgreiche Sicherung ist $age Stunden alt ($(cat "$STATUS_DIR/last_success")).")
    fi
else
    [ -f "$STATUS_DIR/.installed" ] || touch "$STATUS_DIR/.installed"
    age=$(( (now - $(stat -c %Y "$STATUS_DIR/.installed")) / 3600 ))
    if [ "$age" -ge "$MAX_AGE_HOURS" ]; then
        problems+=("Seit der Einrichtung vor $age Stunden gab es keine erfolgreiche Sicherung.")
    fi
fi

if [ -s "$STATUS_DIR/last_error" ] &&
    { [ ! -f "$STATUS_DIR/last_success" ] || [ "$STATUS_DIR/last_error" -nt "$STATUS_DIR/last_success" ]; }; then
    problems+=("Die letzte Sicherung endete mit einem Fehler: $(head -c 1500 "$STATUS_DIR/last_error")")
fi

for marker in check_failed restore_test_failed; do
    if [ -s "$STATUS_DIR/$marker" ]; then
        case "$marker" in
            check_failed) what="Die letzte Integritätsprüfung ist fehlgeschlagen" ;;
            *) what="Der letzte Wiederherstellungstest ist fehlgeschlagen" ;;
        esac
        problems+=("$what: $(head -c 1500 "$STATUS_DIR/$marker")")
    fi
done

if [ ${#problems[@]} -eq 0 ]; then
    rm -f "$ALERT_STAMP"
    exit 0
fi

printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${problems[@]}"

if [ -f "$ALERT_STAMP" ] && [ $(( now - $(stat -c %Y "$ALERT_STAMP") )) -lt 21600 ]; then
    exit 1
fi

body="$(printf '%s\n\n' "${problems[@]}")
Prüfen:
  docker logs --tail 200 $BACKUP_CONTAINER
  docker exec $BACKUP_CONTAINER mandari-backup snapshots
  ls -la $STATUS_DIR

Anleitung: docs/BACKUP.md im mandari-Repository."

if send_mail "[$HOST_LABEL] Backup-Warnung" "$body"; then
    touch "$ALERT_STAMP"
else
    echo "Mailversand fehlgeschlagen" >&2
fi
exit 1
