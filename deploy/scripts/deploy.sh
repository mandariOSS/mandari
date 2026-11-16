#!/bin/sh
# Deploy mit Gesundheitspruefung und automatischem Rueckfall (Issue #285).
#
#   sh deploy/scripts/deploy.sh plan     <tag>   Images ziehen, migrate --plan, check
#   sh deploy/scripts/deploy.sh apply    <tag>   Sicherung, Migration, Umschalten, Pruefung, bei Fehlschlag Rueckfall
#   sh deploy/scripts/deploy.sh verify           nur die Pruefung (Anwendung + Worker) gegen den laufenden Stand
#   sh deploy/scripts/deploy.sh rollback <tag>   von Hand auf einen frueheren Stand
#
# Nicht interaktiv, fuer Cron/CI/Betrieb. Alles Installationsspezifische kommt aus der
# Umgebung, damit auch handgepflegte Installationen mit eigenen Dienstnamen es nutzen:
#   MANDARI_DIR      Installationsverzeichnis mit .env und Compose-Dateien   (Standard /opt/mandari)
#   COMPOSE_FILES    Compose-Dateien, durch Leerzeichen getrennt              (Standard docker-compose.yml)
#   APP_SERVICE      Dienst der Django-Anwendung                              (Standard mandari)
#   WORKER_SERVICES  Dienste, die waehrend der Migration stehen sollen, nach   (Standard: worker,
#                    der Migration VOR der Anwendung starten und nach dem      worker-heavy, worker-live,
#                    Umschalten geprueft werden (Migration -> Worker -> Web)   falls definiert,
#                                                                              und ingestor)
#   WORKER_CHECK_SECONDS  so lange nach dem Start keine Worker-Beendigung      (Standard 60, 0 = aus)
#                    mit Exit-Code ungleich 0; Exit 0 ist planmaessig (Worker enden nach
#                    jedem Durchlauf und werden neu gestartet)
#   WORKER_HEALTH_SECONDS  so lange darauf warten, dass Worker mit Healthcheck   (Standard 120)
#                    (Heartbeat-Datei von events_worker) "healthy" werden
#   DB_SERVICE       PostgreSQL-Dienst fuer die Sicherung                      (Standard postgres)
#   BACKUP_DIR       Ablage der Pre-Deploy-Dumps                               (Standard $MANDARI_DIR/backups)
#   BACKUP_KEEP      wie viele Dumps behalten                                  (Standard 5)
#   BACKUP_MAX_AGE_DAYS  Dumps nach so vielen Tagen loeschen                   (Standard 30, 0 = aus)
#                    Geloeschte Daten verschwinden damit auch aus diesen Sicherungen,
#                    wie aus den Offsite-Backups (restic, 30 Tage). Ohne Deploy greift
#                    die Grenze ueber den taeglichen Aufruf "deploy.sh prune" (Cron).
#   VERIFY_*         siehe deploy/scripts/verify_deploy.py
#   NOTIFY_EMAIL     Empfaenger fuer Rueckfall-Meldungen (ueber den App-Container, Django send_mail)
#   DEPLOY_LOG       Protokoll je Deploy                                       (Standard $MANDARI_DIR/deploy-log.tsv)
#
# Migrationen: Das Skript nutzt django-safemigrate (safemigrate), das nur Migrationen
# einspielt, die mit dem alten Code vertraeglich sind. Ein Rueckfall rollt Code zurueck,
# keine Migrationen; deshalb muessen Migrationen abwaertskompatibel sein.
set -eu
# Alles, was das Skript anlegt (Datenbank-Sicherungen, die Zwischenkopie der .env), ist nur für das eigene Konto
# lesbar: Die Sicherung enthält die ganze Datenbank, die .env alle Schlüssel.
umask 077

MODE="${1:?plan|apply|verify|rollback|prune}"
NEW_TAG="${2:-}"
MANDARI_DIR="${MANDARI_DIR:-/opt/mandari}"
COMPOSE_FILES="${COMPOSE_FILES:-docker-compose.yml}"
APP_SERVICE="${APP_SERVICE:-mandari}"
WORKER_SERVICES="${WORKER_SERVICES:-}"
WORKER_CHECK_SECONDS="${WORKER_CHECK_SECONDS:-60}"
WORKER_HEALTH_SECONDS="${WORKER_HEALTH_SECONDS:-120}"
DB_SERVICE="${DB_SERVICE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-$MANDARI_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-5}"
BACKUP_MAX_AGE_DAYS="${BACKUP_MAX_AGE_DAYS:-30}"
NOTIFY_EMAIL="${NOTIFY_EMAIL:-}"
DEPLOY_LOG="${DEPLOY_LOG:-$MANDARI_DIR/deploy-log.tsv}"
HIER=$(cd "$(dirname "$0")" && pwd)
VERIFY_PY="$HIER/verify_deploy.py"

cd "$MANDARI_DIR"
DC="docker compose"
for f in $COMPOSE_FILES; do DC="$DC -f $f"; done
if [ -z "$WORKER_SERVICES" ]; then
  # Vorgabe: die Worker fuer Ereignisse und Auftraege (manage.py events_worker: worker, worker-heavy,
  # worker-live),
  # soweit die Compose-Datei sie kennt, und der Ingestor. Aeltere, handgepflegte Dateien ohne diese
  # Dienste laufen so unveraendert weiter.
  DIENSTE=$($DC config --services < /dev/null 2>/dev/null || true)
  for svc in worker worker-heavy worker-live; do
    if printf '%s\n' "$DIENSTE" | grep -x "$svc" > /dev/null; then
      WORKER_SERVICES="$WORKER_SERVICES $svc"
    fi
  done
  WORKER_SERVICES="${WORKER_SERVICES# } ingestor"
  WORKER_SERVICES="${WORKER_SERVICES# }"
fi
OLD_TAG=$(grep -E '^IMAGE_TAG=' .env | cut -d= -f2)
[ -n "$OLD_TAG" ] || { echo "FEHLER: IMAGE_TAG fehlt in .env"; exit 1; }
REGISTRY=$(grep -E '^IMAGE_REGISTRY=' .env | cut -d= -f2)
REGISTRY="${REGISTRY:-ghcr.io/mandarioss}"

app_container() { $DC ps -q "$APP_SERVICE" | head -1; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Vor-Deploy-Sicherungen begrenzen: hoechstens BACKUP_KEEP Stueck, keine aelter als BACKUP_MAX_AGE_DAYS Tage
alte_sicherungen_loeschen() {
  ls -1t "$BACKUP_DIR"/pre-deploy-*.dump 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
  if [ "$BACKUP_MAX_AGE_DAYS" -gt 0 ] && [ -d "$BACKUP_DIR" ]; then
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'pre-deploy-*.dump' -mtime +$((BACKUP_MAX_AGE_DAYS - 1)) \
      -exec sh -c 'for f; do echo "Sicherung aelter als Hoechstalter geloescht: $f"; rm -f "$f"; done' sh {} +
  fi
}

run_manage() {
  # Einmalcontainer mit dem NEUEN Image, ohne Abhaengigkeiten hochzufahren
  logdatei="$1"; shift
  if ! IMAGE_TAG="$NEW_TAG" $DC run --rm --no-deps -T "$APP_SERVICE" python manage.py "$@" < /dev/null > "$logdatei" 2>&1; then
    grep -vE '^\{"ts"' "$logdatei" | tail -30
    echo "FEHLER: manage.py $*"
    return 1
  fi
  grep -vE '^\{"ts"' "$logdatei" | tail -30 || true
}

verify() {
  # Anwendungspruefung im laufenden App-Container; Umgebung VERIFY_* durchreichen
  c=$(app_container)
  [ -n "$c" ] || { echo "FEHLER: App-Container nicht gefunden"; return 1; }
  docker cp "$VERIFY_PY" "$c:/tmp/verify_deploy.py"
  env_args=""
  for v in VERIFY_HOST VERIFY_PATHS VERIFY_DEMO_PAGES VERIFY_MIN_BYTES; do
    eval wert="\${$v:-}"
    [ -n "$wert" ] && env_args="$env_args -e $v=$wert"
  done
  ausgabe=$(mktemp)
  # shellcheck disable=SC2086
  docker exec $env_args "$c" python manage.py shell -c "exec(open('/tmp/verify_deploy.py').read())" > "$ausgabe" 2>&1 || true
  grep -vE '^\{"ts"|objects imported' "$ausgabe" || true
  grep -q '^ERGEBNIS: alle' "$ausgabe"
  rc=$?
  rm -f "$ausgabe"
  return $rc
}

worker_container() {
  # Je Worker-Dienst eine Zeile "dienst container-id", auch fuer beendete Container;
  # "dienst -", wenn der Dienst keinen Container hat
  for s in $WORKER_SERVICES; do
    ids=$($DC ps -a -q "$s" < /dev/null 2>/dev/null || true)
    [ -n "$ids" ] || { echo "$s -"; continue; }
    for id in $ids; do echo "$s $id"; done
  done
}

gesundheit() {
  # Healthcheck-Zustand eines Containers (starting, healthy, unhealthy) oder leer ohne Healthcheck
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1" 2>/dev/null || true
}

worker_beobachten() {
  # Zeichnet im Hintergrund jedes Beenden (Docker-Ereignis "die") der Worker-Container ab
  # Zeitpunkt $1 (Unix-Sekunden) auf, bis WORKER_CHECK_SECONDS nach diesem Aufruf. --since holt
  # Beendigungen zwischen Start und Aufruf aus dem Ereignispuffer nach. Laeuft parallel zur
  # Anwendungspruefung; worker_pruefen wartet auf das Ende und wertet aus.
  WORKER_EREIGNISSE=$(mktemp)
  WORKER_PID=""
  [ "$WORKER_CHECK_SECONDS" -gt 0 ] || return 0
  filter=""
  for id in $(worker_container | awk '$2 != "-" { print $2 }'); do
    filter="$filter --filter container=$id"
  done
  [ -n "$filter" ] || return 0
  # shellcheck disable=SC2086
  docker events --since "$1" --until "$(( $(date +%s) + WORKER_CHECK_SECONDS ))" \
    --filter type=container --filter event=die $filter \
    --format '{{.Actor.Attributes.name}} {{.Actor.Attributes.exitCode}}' > "$WORKER_EREIGNISSE" &
  WORKER_PID=$!
}

worker_pruefen() {
  # Die Worker (Ingestor, OCR-Worker, ...) beenden sich nach jedem Durchlauf planmaessig mit
  # Exit 0 und werden neu gestartet; Exit 0 ist daher kein Fehler, ebenso wenig die Zahl der
  # Neustarts. Fehlschlag: jede Beendigung mit Exit-Code ungleich 0 im Zeitfenster (z. B. ein
  # Image, das beim Start mit einem Importfehler abbricht und in einer Neustart-Schleife haengt),
  # ein Endzustand ausser "running" mit Exit-Code ungleich 0 und ein Dienst ohne Container.
  if [ "$WORKER_CHECK_SECONDS" -le 0 ]; then
    rm -f "$WORKER_EREIGNISSE"
    return 0
  fi
  echo "Pruefe Worker ($WORKER_SERVICES), ${WORKER_CHECK_SECONDS} s nach dem Start"
  if [ -n "$WORKER_PID" ] && ! wait "$WORKER_PID"; then
    echo "  WARNUNG Docker-Ereignisse nicht lesbar, pruefe nur den Endzustand"
  fi
  fehler=0
  while read -r name code; do
    [ -n "$name" ] || continue
    if [ "$code" = 0 ]; then
      echo "  OK   $name beendet mit Exit 0 (planmaessig)"
    else
      echo "  FAIL $name beendet mit Exit $code"
      fehler=1
    fi
  done < "$WORKER_EREIGNISSE"
  rm -f "$WORKER_EREIGNISSE"

  for zeile in $(worker_container | tr ' ' ':'); do
    dienst=${zeile%%:*}
    id=${zeile#*:}
    if [ "$id" = "-" ]; then
      echo "  FAIL $dienst: kein Container"
      fehler=1
      continue
    fi
    zustand=$(docker inspect -f '{{.Name}} {{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}' "$id" 2>/dev/null || true)
    if [ -z "$zustand" ]; then
      echo "  FAIL $dienst: Zustand von Container $id nicht lesbar"
      fehler=1
      continue
    fi
    read -r cname status code neustarts <<EOF
$zustand
EOF
    cname=${cname#/}
    if [ "$status" = running ] || { [ "$code" = 0 ] && { [ "$status" = exited ] || [ "$status" = restarting ]; }; }; then
      echo "  OK   $cname: $status (Exit $code, Neustarts insgesamt $neustarts)"
    else
      echo "  FAIL $cname: $status, Exit $code (Neustarts insgesamt $neustarts)"
      fehler=1
    fi
  done
  # Gesundheitspruefung per Heartbeat (Issue #574): Container mit Healthcheck (die Worker fuer
  # Ereignisse, Auftraege und Zeitplaene erneuern eine Heartbeat-Datei, solange jede Rolle arbeitet)
  # muessen "healthy" werden. Container ohne Healthcheck (Ingestor) zaehlen nur mit dem Zustand oben.
  for zeile in $(worker_container | tr ' ' ':'); do
    dienst=${zeile%%:*}
    id=${zeile#*:}
    [ "$id" != "-" ] || continue
    gesund=$(gesundheit "$id")
    [ -n "$gesund" ] || continue
    gewartet=0
    while [ "$gesund" = starting ] && [ "$gewartet" -lt "$WORKER_HEALTH_SECONDS" ]; do
      sleep 5
      gewartet=$((gewartet + 5))
      gesund=$(gesundheit "$id")
    done
    if [ "$gesund" = healthy ]; then
      echo "  OK   $dienst: healthy (Heartbeat)"
    else
      echo "  FAIL $dienst: ${gesund:-unbekannt} (Heartbeat, nach ${gewartet} s)"
      fehler=1
    fi
  done
  echo "ERGEBNIS Worker: $([ "$fehler" -eq 0 ] && echo 'alle Pruefungen bestanden' || echo 'Pruefung fehlgeschlagen')"
  [ "$fehler" -eq 0 ]
}

notify() {
  betreff="$1"; text="$2"
  [ -n "$NOTIFY_EMAIL" ] || return 0
  c=$(app_container)
  [ -n "$c" ] || return 0
  docker exec "$c" python manage.py shell -c "
from django.core.mail import send_mail
send_mail('$betreff', '''$text''', None, ['$NOTIFY_EMAIL'])" >/dev/null 2>&1 || true
}

warte_auf_live() {
  # Sekunden ab Beginn der Umschaltung, bis /health/ im App-Container wieder 200 liefert
  # (Unterbrechung). Laeuft ueber compose exec, braucht keinen veroeffentlichten Port.
  # /health/ statt /health/live/, weil auch ein aelteres Rueckfall-Image es kennt.
  start="$1"
  i=0
  while [ $i -lt 300 ]; do
    if $DC exec -T "$APP_SERVICE" curl -sf -m 2 -o /dev/null "http://localhost:8000/health/" >/dev/null 2>&1; then
      echo $(( $(date +%s) - start )); return 0
    fi
    i=$((i + 1)); sleep 1
  done
  echo ">300"
}

switch_to() {
  # Reihenfolge Migration -> Worker -> Web: Die Worker laufen schon mit dem Stand $1, wenn die
  # Anwendung umschaltet; Auftraege und Ereignisse der neuen Webprozesse bleiben nicht liegen.
  tag="$1"
  # Die Zwischenkopie enthält alle Schlüssel: jedes Mal neu angelegt (mktemp, nur für das eigene Konto), nie eine
  # liegen gebliebene Datei mit anderen Rechten wiederverwendet
  neu=$(mktemp .env.neu.XXXXXX)
  if ! sed "s/^IMAGE_TAG=.*/IMAGE_TAG=$tag/" .env > "$neu"; then
    rm -f "$neu"
    echo "FEHLER: IMAGE_TAG in .env nicht umgestellt"
    return 1
  fi
  if ! cat "$neu" > .env; then
    # .env kann jetzt unvollständig sein: Die vollständige Kopie bleibt liegen (nur für das eigene Konto lesbar)
    echo "FEHLER: .env nicht geschrieben. Vollständige Kopie: $MANDARI_DIR/$neu"
    return 1
  fi
  rm -f "$neu"
  # shellcheck disable=SC2086
  $DC up -d --no-deps $WORKER_SERVICES < /dev/null
  $DC up -d --no-deps --wait "$APP_SERVICE" < /dev/null
}

protokoll() {
  # Zeit, alt, neu, Ergebnis, Unterbrechung(s), Dauer(s)
  mkdir -p "$(dirname "$DEPLOY_LOG")"
  [ -f "$DEPLOY_LOG" ] || printf 'zeit\talt\tneu\tergebnis\tunterbrechung_s\tdauer_s\n' > "$DEPLOY_LOG"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" >> "$DEPLOY_LOG"
}

case "$MODE" in
  prune)
    alte_sicherungen_loeschen
    ;;

  plan)
    [ -n "$NEW_TAG" ] || { echo "Tag fehlt"; exit 1; }
    log "Alt: $OLD_TAG  Neu: $NEW_TAG"
    docker pull -q "$REGISTRY/mandari:$NEW_TAG"
    docker pull -q "$REGISTRY/ingestor:$NEW_TAG" || echo "(kein Ingestor-Image mit diesem Tag)"
    echo "--- migrate --plan"; run_manage /tmp/deploy_plan.log migrate --plan
    echo "--- check";          run_manage /tmp/deploy_check.log check
    df -h "$MANDARI_DIR" | tail -1
    ;;

  verify)
    worker_beobachten "$(date +%s)"
    GRUND=""
    verify || GRUND="die Anwendungspruefung"
    worker_pruefen || GRUND="${GRUND:+$GRUND und }die Worker-Pruefung"
    [ -z "$GRUND" ] || { log "Nicht bestanden: $GRUND ($OLD_TAG)"; exit 1; }
    log "Pruefung bestanden ($OLD_TAG)"
    ;;

  rollback)
    [ -n "$NEW_TAG" ] || { echo "Tag fehlt"; exit 1; }
    log "Rueckfall $OLD_TAG -> $NEW_TAG"
    START=$(date +%s)
    switch_to "$NEW_TAG"
    protokoll "$OLD_TAG" "$NEW_TAG" "rollback-manuell" "" "$(( $(date +%s) - START ))"
    verify || true
    ;;

  apply)
    [ -n "$NEW_TAG" ] || { echo "Tag fehlt"; exit 1; }
    START=$(date +%s)
    log "Deploy $OLD_TAG -> $NEW_TAG"

    mkdir -p "$BACKUP_DIR"
    # umask wirkt nur auf Neues: Verzeichnis und Sicherungen früherer Läufe ebenfalls schließen. Nur wenn das
    # Verzeichnis dem eigenen Konto gehört: Nach chmod 700 kann dann kein anderes Konto darin Dateien gegen
    # symbolische Links tauschen, während die Rechte geändert werden. Gehört es einem anderen Konto, bleiben
    # ältere Dateien unverändert.
    if [ -O "$BACKUP_DIR" ]; then
      chmod 700 "$BACKUP_DIR"
      for alt in "$BACKUP_DIR"/pre-deploy-*.dump; do
        # Nur gewöhnliche Dateien: Über einen symbolischen Link änderte chmod die Rechte der Zieldatei
        if [ -f "$alt" ] && [ ! -L "$alt" ]; then
          chmod 600 "$alt"
        fi
      done
    else
      echo "Hinweis: $BACKUP_DIR gehört einem anderen Konto, Rechte älterer Sicherungen bleiben unverändert"
    fi
    BACKUP="$BACKUP_DIR/pre-deploy-$NEW_TAG-$(date -u +%Y%m%d-%H%M).dump"
    $DC exec -T "$DB_SERVICE" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' < /dev/null > "$BACKUP"
    [ -s "$BACKUP" ] || { echo "FEHLER: Sicherung leer"; exit 1; }
    log "Sicherung $BACKUP ($(du -h "$BACKUP" | cut -f1))"
    # alte Dumps begrenzen (Anzahl und Hoechstalter)
    alte_sicherungen_loeschen

    # shellcheck disable=SC2086
    $DC stop $WORKER_SERVICES < /dev/null
    echo "--- safemigrate"
    if ! run_manage /tmp/deploy_migrate.log safemigrate --noinput; then
      echo "Migration fehlgeschlagen – Code bleibt auf $OLD_TAG"
      # shellcheck disable=SC2086
      $DC up -d --no-deps $WORKER_SERVICES < /dev/null
      protokoll "$OLD_TAG" "$NEW_TAG" "migration-fehlgeschlagen" "" "$(( $(date +%s) - START ))"
      notify "[deploy] Migration fehlgeschlagen ($NEW_TAG)" "Migration auf $NEW_TAG fehlgeschlagen, Code bleibt auf $OLD_TAG. Log: /tmp/deploy_migrate.log"
      exit 1
    fi

    T0=$(date +%s)
    switch_to "$NEW_TAG"
    # Worker-Beobachtung laeuft ab dem Umschalten parallel zur Anwendungspruefung
    worker_beobachten "$T0"
    UNTERBRECHUNG=$(warte_auf_live "$T0")
    log "Umgeschaltet, Unterbrechung ${UNTERBRECHUNG:-?} s"

    GRUND=""
    verify || GRUND="die Anwendungspruefung"
    worker_pruefen || GRUND="${GRUND:+$GRUND und }die Worker-Pruefung"
    if [ -z "$GRUND" ]; then
      protokoll "$OLD_TAG" "$NEW_TAG" "ok" "$UNTERBRECHUNG" "$(( $(date +%s) - START ))"
      log "Deploy $NEW_TAG bestanden. Rueckfall: sh $0 rollback $OLD_TAG  (Sicherung $BACKUP)"
    else
      log "PRUEFUNG FEHLGESCHLAGEN ($GRUND) – Rueckfall auf $OLD_TAG"
      T1=$(date +%s)
      switch_to "$OLD_TAG"
      R=$(warte_auf_live "$T1")
      protokoll "$OLD_TAG" "$NEW_TAG" "rollback-automatisch" "$UNTERBRECHUNG+$R" "$(( $(date +%s) - START ))"
      notify "[deploy] Rueckfall auf $OLD_TAG" "Deploy $NEW_TAG hat $GRUND nicht bestanden und wurde automatisch auf $OLD_TAG zurueckgesetzt. Migrationen bleiben eingespielt (safemigrate). Sicherung: $BACKUP"
      verify || true
      exit 1
    fi
    ;;

  *) echo "Modus plan|apply|verify|rollback"; exit 1 ;;
esac
