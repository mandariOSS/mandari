#!/bin/sh
# Protokollierung auf einem mandari-Host einrichten (Issue #263, BSI OPS.1.1.5).
# Als root ausführen. Idempotent: mehrfacher Aufruf ändert nichts Zusätzliches.
#
#   sudo sh deploy/logging/install.sh [MANDARI_CONTAINER] [ALERT_EMAIL]
#
# Richtet ein:
#   1. journald-Drop-in (persistent, 90 Tage, höchstens 2 GB)
#   2. logrotate für die Cron-Logdateien unter /var/log/mandari-*.log
#   3. stündliche Überlaufwarnung (journal-alert.sh) per Cron
# Nicht enthalten (siehe docs/PROTOKOLLE.md): Compose-Override für den journald-Treiber
# beim nächsten Deploy und die zeitliche Begrenzung der Caddy-Zugriffslogs.
set -eu

HIER=$(cd "$(dirname "$0")" && pwd)
CONTAINER="${1:-mandari}"
EMPFAENGER="${2:-admin@mandari.de}"

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen (sudo)."; exit 1; }

echo "1/3 journald-Drop-in"
install -d -m 755 /etc/systemd/journald.conf.d
install -m 644 "$HIER/journald-mandari.conf" /etc/systemd/journald.conf.d/mandari.conf
install -d -m 2755 -g systemd-journal /var/log/journal
systemctl restart systemd-journald
journalctl --disk-usage

echo "2/3 logrotate"
install -m 644 "$HIER/logrotate-mandari" /etc/logrotate.d/mandari
logrotate -d /etc/logrotate.d/mandari >/dev/null 2>&1 && echo "  logrotate-Konfiguration gültig"

echo "3/3 Überlaufwarnung"
install -m 755 "$HIER/journal-alert.sh" /usr/local/bin/journal-alert.sh
ZEILE="30 * * * * MANDARI_CONTAINER=$CONTAINER ALERT_EMAIL=$EMPFAENGER /usr/local/bin/journal-alert.sh"
( crontab -l 2>/dev/null | grep -v 'journal-alert.sh' ; echo "$ZEILE" ) | crontab -
echo "  Cron: $ZEILE"

cat <<EOF

Fertig. Noch offen (beim nächsten Deploy):
  - Compose mit deploy/logging/docker-compose.journald.yml starten, damit die
    Container-Logs ins Journal laufen (Dienstnamen bei eigener Compose anpassen).
  - Caddy: in den Zugriffslog-Blöcken 'roll_keep_for 336h' setzen (14 Tage), dann 'caddy reload'.
  - Backup: /var/log/journal als Quelle einhängen (BACKUP_SRC_JOURNAL, siehe docs/BACKUP.md).
EOF
