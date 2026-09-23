#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Startet Container neu, die Docker als "unhealthy" meldet (Issue #344).
#
# Docker Compose tut das nicht selbst: Ein ungesunder Container laeuft einfach weiter.
# Die Liveness der Anwendung (/health/live/) wird rot, wenn ihr Datenbank-Pool
# festgefahren ist -- dann hilft nur ein Neustart. Dieses Skript regelmaessig starten,
# z. B. jede Minute per systemd-Timer oder Cron:
#
#   sh deploy/scripts/restart-unhealthy.sh                 # alle Container mit Label mandari.autoheal=true
#   sh deploy/scripts/restart-unhealthy.sh mandari-web     # nur die genannten Container
#
# Schutz gegen Neustart-Schleifen: Ein Container, der vor weniger als
# RESTART_MIN_UPTIME Sekunden (Standard 300) gestartet wurde, wird nicht erneut
# neu gestartet. Jeder Neustart landet im Systemprotokoll (Kennung mandari-autoheal).
set -eu

MIN_UPTIME="${RESTART_MIN_UPTIME:-300}"

if [ "$#" -gt 0 ]; then
    kandidaten="$*"
else
    kandidaten=$(docker ps --filter "label=mandari.autoheal=true" --format '{{.Names}}')
fi

jetzt=$(date +%s)
for name in $kandidaten; do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null || true)
    [ "$status" = "unhealthy" ] || continue

    gestartet=$(docker inspect -f '{{.State.StartedAt}}' "$name")
    seit=$(( jetzt - $(date -d "$gestartet" +%s) ))
    if [ "$seit" -lt "$MIN_UPTIME" ]; then
        meldung="Container $name ist unhealthy, laeuft aber erst ${seit}s -- kein erneuter Neustart"
    else
        meldung="Container $name ist unhealthy -- Neustart"
        docker restart "$name" >/dev/null
    fi
    echo "$(date -Is) $meldung"
    logger -t mandari-autoheal -p user.err "$meldung" 2>/dev/null || true
done
