#!/bin/sh
# Deploy mit Gesundheitspruefung und automatischem Rueckfall (Issue #285).
#
#   sh deploy/scripts/deploy.sh plan     <tag>   Images ziehen, migrate --plan, check
#   sh deploy/scripts/deploy.sh apply    <tag>   Sicherung, Migration, Umschalten, Pruefung, bei Fehlschlag Rueckfall
#   sh deploy/scripts/deploy.sh verify           nur die Anwendungspruefung gegen den laufenden Stand
#   sh deploy/scripts/deploy.sh rollback <tag>   von Hand auf einen frueheren Stand
#
# Nicht interaktiv, fuer Cron/CI/Betrieb. Alles Installationsspezifische kommt aus der
# Umgebung, damit auch handgepflegte Installationen mit eigenen Dienstnamen es nutzen:
#   MANDARI_DIR      Installationsverzeichnis mit .env und Compose-Dateien   (Standard /opt/mandari)
#   COMPOSE_FILES    Compose-Dateien, durch Leerzeichen getrennt              (Standard docker-compose.yml)
#   APP_SERVICE      Dienst der Django-Anwendung                              (Standard mandari)
#   WORKER_SERVICES  Dienste, die waehrend der Migration stehen sollen         (Standard ingestor)
#   DB_SERVICE       PostgreSQL-Dienst fuer die Sicherung                      (Standard postgres)
#   BACKUP_DIR       Ablage der Pre-Deploy-Dumps                               (Standard $MANDARI_DIR/backups)
#   BACKUP_KEEP      wie viele Dumps behalten                                  (Standard 5)
#   VERIFY_*         siehe deploy/scripts/verify_deploy.py
#   NOTIFY_EMAIL     Empfaenger fuer Rueckfall-Meldungen (ueber den App-Container, Django send_mail)
#   DEPLOY_LOG       Protokoll je Deploy                                       (Standard $MANDARI_DIR/deploy-log.tsv)
#
# Migrationen: Das Skript nutzt django-safemigrate (safemigrate), das nur Migrationen
# einspielt, die mit dem alten Code vertraeglich sind. Ein Rueckfall rollt Code zurueck,
# keine Migrationen; deshalb muessen Migrationen abwaertskompatibel sein.
set -eu

MODE="${1:?plan|apply|verify|rollback}"
NEW_TAG="${2:-}"
MANDARI_DIR="${MANDARI_DIR:-/opt/mandari}"
COMPOSE_FILES="${COMPOSE_FILES:-docker-compose.yml}"
APP_SERVICE="${APP_SERVICE:-mandari}"
WORKER_SERVICES="${WORKER_SERVICES:-ingestor}"
DB_SERVICE="${DB_SERVICE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-$MANDARI_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-5}"
NOTIFY_EMAIL="${NOTIFY_EMAIL:-}"
DEPLOY_LOG="${DEPLOY_LOG:-$MANDARI_DIR/deploy-log.tsv}"
HIER=$(cd "$(dirname "$0")" && pwd)
VERIFY_PY="$HIER/verify_deploy.py"

cd "$MANDARI_DIR"
DC="docker compose"
for f in $COMPOSE_FILES; do DC="$DC -f $f"; done
OLD_TAG=$(grep -E '^IMAGE_TAG=' .env | cut -d= -f2)
[ -n "$OLD_TAG" ] || { echo "FEHLER: IMAGE_TAG fehlt in .env"; exit 1; }
REGISTRY=$(grep -E '^IMAGE_REGISTRY=' .env | cut -d= -f2)
REGISTRY="${REGISTRY:-ghcr.io/mandarioss}"

app_container() { $DC ps -q "$APP_SERVICE" | head -1; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

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
  tag="$1"
  sed "s/^IMAGE_TAG=.*/IMAGE_TAG=$tag/" .env > .env.neu && cat .env.neu > .env && rm -f .env.neu
  $DC up -d --no-deps --wait "$APP_SERVICE" < /dev/null
  # shellcheck disable=SC2086
  $DC up -d --no-deps $WORKER_SERVICES < /dev/null
}

protokoll() {
  # Zeit, alt, neu, Ergebnis, Unterbrechung(s), Dauer(s)
  mkdir -p "$(dirname "$DEPLOY_LOG")"
  [ -f "$DEPLOY_LOG" ] || printf 'zeit\talt\tneu\tergebnis\tunterbrechung_s\tdauer_s\n' > "$DEPLOY_LOG"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" >> "$DEPLOY_LOG"
}

case "$MODE" in
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
    verify && log "Pruefung bestanden ($OLD_TAG)"
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
    BACKUP="$BACKUP_DIR/pre-deploy-$NEW_TAG-$(date -u +%Y%m%d-%H%M).dump"
    $DC exec -T "$DB_SERVICE" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' < /dev/null > "$BACKUP"
    [ -s "$BACKUP" ] || { echo "FEHLER: Sicherung leer"; exit 1; }
    log "Sicherung $BACKUP ($(du -h "$BACKUP" | cut -f1))"
    # alte Dumps begrenzen
    ls -1t "$BACKUP_DIR"/pre-deploy-*.dump 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f

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
    UNTERBRECHUNG=$(warte_auf_live "$T0")
    log "Umgeschaltet, Unterbrechung ${UNTERBRECHUNG:-?} s"

    if verify; then
      protokoll "$OLD_TAG" "$NEW_TAG" "ok" "$UNTERBRECHUNG" "$(( $(date +%s) - START ))"
      log "Deploy $NEW_TAG bestanden. Rueckfall: sh $0 rollback $OLD_TAG  (Sicherung $BACKUP)"
    else
      log "PRUEFUNG FEHLGESCHLAGEN – Rueckfall auf $OLD_TAG"
      T1=$(date +%s)
      switch_to "$OLD_TAG"
      R=$(warte_auf_live "$T1")
      protokoll "$OLD_TAG" "$NEW_TAG" "rollback-automatisch" "$UNTERBRECHUNG+$R" "$(( $(date +%s) - START ))"
      notify "[deploy] Rueckfall auf $OLD_TAG" "Deploy $NEW_TAG hat die Anwendungspruefung nicht bestanden und wurde automatisch auf $OLD_TAG zurueckgesetzt. Migrationen bleiben eingespielt (safemigrate). Sicherung: $BACKUP"
      verify || true
      exit 1
    fi
    ;;

  *) echo "Modus plan|apply|verify|rollback"; exit 1 ;;
esac
