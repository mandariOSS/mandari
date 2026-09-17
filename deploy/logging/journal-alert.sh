#!/bin/sh
# Überlaufwarnung für das Journal (Issue #263, BSI CON.10.A17: Protokolldaten-Überlauf überwachen).
#
# Meldet per Mail (über den mandari-Container, Django send_mail), wenn das Journal
# mehr als SCHWELLE Prozent seiner Obergrenze SystemMaxUse belegt – dann kürzt
# journald nach Größe statt nach Zeit, und die zugesagte Aufbewahrung gilt nicht mehr.
# Höchstens eine Mail je 6 Stunden. Stündlich per Cron aufrufen (deploy/logging/install.sh).
#
# Umgebung: MANDARI_CONTAINER (Standard mandari), ALERT_EMAIL (Standard admin@mandari.de),
#           JOURNAL_MAX (Standard: SystemMaxUse aus journald.conf.d, sonst 2G), SCHWELLE (90)
set -eu

CONTAINER="${MANDARI_CONTAINER:-mandari}"
EMPFAENGER="${ALERT_EMAIL:-admin@mandari.de}"
SCHWELLE="${SCHWELLE:-90}"
STAMP=/var/run/journal-alert-sent

konf_max() {
  cat /etc/systemd/journald.conf.d/*.conf /etc/systemd/journald.conf 2>/dev/null \
    | grep -E '^SystemMaxUse=' | tail -1 | cut -d= -f2
}

zu_bytes() {
  # 2G / 512M / 100K → Bytes
  wert=$(echo "$1" | tr -dc '0-9')
  einheit=$(echo "$1" | tr -dc 'KMGkmg' | head -c1 | tr 'kmg' 'KMG')
  case "$einheit" in
    K) echo $((wert * 1024)) ;;
    M) echo $((wert * 1024 * 1024)) ;;
    G) echo $((wert * 1024 * 1024 * 1024)) ;;
    *) echo "$wert" ;;
  esac
}

MAX="${JOURNAL_MAX:-$(konf_max)}"
[ -n "$MAX" ] || MAX=2G
MAX_BYTES=$(zu_bytes "$MAX")

# "Archived and active journals take up 51.2M in the file system."
BELEGT=$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | head -1)
[ -n "$BELEGT" ] || exit 0
BELEGT_BYTES=$(zu_bytes "$(echo "$BELEGT" | cut -d. -f1)$(echo "$BELEGT" | tr -dc 'KMG')")
PROZENT=$((BELEGT_BYTES * 100 / MAX_BYTES))

[ "$PROZENT" -ge "$SCHWELLE" ] || exit 0
if [ -f "$STAMP" ] && [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -lt 21600 ]; then exit 0; fi

AELTESTER=$(journalctl -o short-iso -q 2>/dev/null | head -1 | cut -c1-19)
docker exec "$CONTAINER" python manage.py shell -c "
from django.core.mail import send_mail
send_mail(
    '[$(hostname)] Journal-Warnung: ${PROZENT}% von ${MAX} belegt',
    'Das systemd-Journal auf $(hostname) belegt ${BELEGT} von hoechstens ${MAX} (${PROZENT}%).\n'
    'Ab der Obergrenze kuerzt journald nach Groesse statt nach Zeit; die zugesagte Aufbewahrung von 90 Tagen gilt dann nicht mehr.\n\n'
    'Aeltester Eintrag: ${AELTESTER}\n\n'
    'Pruefen:  journalctl --disk-usage; journalctl -S -1h -p warning\n'
    'Ursache eingrenzen: journalctl -S -24h -o cat | cut -c1-60 | sort | uniq -c | sort -rn | head\n'
    'Siehe docs/PROTOKOLLE.md im mandari-Repo.',
    None, ['${EMPFAENGER}'],
)" >/dev/null 2>&1 && touch "$STAMP"
