#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Staging als Pruefstand (Issue #737): zieht den neuesten gruenen dev-Stand nach.
#
#   sh deploy/scripts/staging_update.sh           pruefen und bei Bedarf deployen (fuer den systemd-Timer)
#   sh deploy/scripts/staging_update.sh pruefen   nur anzeigen, was ein Lauf taete; aendert nichts
#
# Pull-basiert: Der Server fragt die oeffentliche GitHub-API, GitHub braucht keinen Zugang zum Server.
#
# Ablauf eines Laufs:
#   1. Sperre (immer nur ein Lauf), Schutz gegen eine falsch konfigurierte Zielumgebung
#   2. Ziel ermitteln: das neueste Commit auf $GITHUB_BRANCH, fuer das der Release-Lauf (Images gebaut
#      und veroeffentlicht) UND die CI erfolgreich waren. Als CI zaehlt der Lauf in der Merge-Queue fuer
#      genau dieses Commit oder ein Lauf direkt auf dem Zweig (von Hand oder nachts gestartet, aeltere
#      Laeufe nach Push). Seit Issue #935 startet ein Push auf dev keine CI mehr: Auf dev landet genau
#      das Commit, das die Queue geprueft hat (gleiche Commit-Kennung).
#   3. Mit dem laufenden Tag (IMAGE_TAG in $MANDARI_DIR/.env) vergleichen. Gleich oder aelter: Ende.
#   4. deploy.sh plan <tag> (Images ziehen, migrate --plan, check), dann deploy.sh apply <tag>
#      (Sicherung, Migration, Umschalten, Anwendungs- und Worker-Pruefung, bei Fehlschlag Rueckfall).
#   5. Die gesamte Ausgabe landet als Pruefprotokoll unter $STATE_DIR/protokolle/. Ein Tag, dessen
#      apply gescheitert ist, wird nicht noch einmal versucht; nach drei gescheiterten plan-Laeufen
#      (z. B. Image nicht abrufbar) ebenfalls nicht. Der naechste neuere Stand wird wieder versucht.
#
# Idempotent: Ohne neuen Stand passiert nichts (Exit 0). Exit 1 bei Fehlern (API nicht erreichbar,
# Konfiguration, gescheiterter Deploy); systemd zeigt den Lauf dann als fehlgeschlagen.
#
# Umgebung (systemd: EnvironmentFile, Vorlage unter deploy/systemd/):
#   MANDARI_DIR          Installationsverzeichnis der Staging-Umgebung (Pflicht, keine Vorgabe)
#   DEPLOY_ENV           Umgebung fuer deploy.sh: Dienstnamen, Compose-Dateien, Empfaenger
#                        (Standard $MANDARI_DIR/deploy.env, falls vorhanden)
#   DEPLOY_SCRIPT        Standard: deploy.sh im Verzeichnis dieses Skripts
#   GITHUB_REPO          Standard mandariOSS/mandari
#   GITHUB_BRANCH        Standard dev
#   CI_WORKFLOW          Standard pr-check.yml
#   RELEASE_WORKFLOW     Standard release.yml
#   GITHUB_API           Standard https://api.github.com
#   GITHUB_TOKEN         optional, nur fuer ein hoeheres Abfragelimit (ohne Token 60 Abfragen je Stunde;
#                        ein Lauf braucht drei, bei neuem Stand vier). Das Token braucht keine Rechte.
#   STATE_DIR            Zustand, Sperre, Log, Pruefprotokolle (Standard $MANDARI_DIR/staging-update)
#   PROTOKOLLE_BEHALTEN  so viele Pruefprotokolle behalten (Standard 50)
#
# Schutz: Die Zielumgebung muss sich in $MANDARI_DIR/.env mit der Zeile MANDARI_UMGEBUNG=staging
# ausweisen. Fehlt sie, bricht das Skript ab, bevor es etwas veraendert. So kann eine falsch gesetzte
# Variable nie eine Produktion auf den Entwicklungsstand ziehen.
#
# Pausieren: Datei $STATE_DIR/pause anlegen (touch); entfernen setzt die Aktualisierung fort.
# Erneuter Versuch eines gescheiterten Tags: Zeile aus $STATE_DIR/fehlgeschlagen entfernen.
#
# Voraussetzungen: curl, jq; fuer den Deploy docker (ueber deploy.sh).
set -eu

MODUS="${1:-lauf}"
case "$MODUS" in
  lauf|pruefen) ;;
  *) echo "Aufruf: $0 [pruefen]"; exit 1 ;;
esac

if [ -z "${MANDARI_DIR:-}" ]; then
  echo "FEHLER: MANDARI_DIR fehlt (Installationsverzeichnis der Staging-Umgebung)"
  exit 1
fi
STAGING_DIR="$MANDARI_DIR"
HIER=$(cd "$(dirname "$0")" && pwd)
DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-$HIER/deploy.sh}"
DEPLOY_ENV="${DEPLOY_ENV:-$STAGING_DIR/deploy.env}"
GITHUB_REPO="${GITHUB_REPO:-mandariOSS/mandari}"
GITHUB_BRANCH="${GITHUB_BRANCH:-dev}"
CI_WORKFLOW="${CI_WORKFLOW:-pr-check.yml}"
RELEASE_WORKFLOW="${RELEASE_WORKFLOW:-release.yml}"
GITHUB_API="${GITHUB_API:-https://api.github.com}"
STATE_DIR="${STATE_DIR:-$STAGING_DIR/staging-update}"
PROTOKOLLE_BEHALTEN="${PROTOKOLLE_BEHALTEN:-50}"
PLAN_VERSUCHE_MAX=3

LOG_FILE="$STATE_DIR/staging-update.log"
SPERRE="$STATE_DIR/lauf.sperre"
PAUSE="$STATE_DIR/pause"
FEHLGESCHLAGEN="$STATE_DIR/fehlgeschlagen"
PLAN_FEHLER="$STATE_DIR/plan-fehler"
STATUS="$STATE_DIR/status"
PROTOKOLL_DIR="$STATE_DIR/protokolle"

fehler() { echo "FEHLER: $*"; exit 1; }

for programm in curl jq; do
  command -v "$programm" > /dev/null 2>&1 || fehler "$programm fehlt (z. B. apt install $programm)"
done

[ -f "$STAGING_DIR/.env" ] || fehler "$STAGING_DIR/.env fehlt"
grep -Eq '^MANDARI_UMGEBUNG="?staging"?[[:space:]]*$' "$STAGING_DIR/.env" \
  || fehler "$STAGING_DIR/.env weist sich nicht als Staging aus (Zeile MANDARI_UMGEBUNG=staging fehlt) – Abbruch ohne Aenderung"
[ "$MODUS" = pruefen ] || [ -f "$DEPLOY_SCRIPT" ] || fehler "Deploy-Skript $DEPLOY_SCRIPT fehlt"

mkdir -p "$STATE_DIR" "$PROTOKOLL_DIR"
# Log klein halten: ab 1 MiB eine Generation aufheben
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 1048576 ]; then
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

log() {
  zeile="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  printf '%s\n' "$zeile"
  printf '%s\n' "$zeile" >> "$LOG_FILE"
}

# --- Sperre: ein Lauf zur Zeit (mkdir ist atomar; verwaiste Sperren werden uebernommen) --------
ARBEIT=""
# shellcheck disable=SC2329  # ueber trap aufgerufen
aufraeumen() {
  [ -z "$ARBEIT" ] || rm -rf "$ARBEIT"
  if [ "${SPERRE_GEHALTEN:-}" = ja ]; then rm -rf "$SPERRE"; fi
}
trap aufraeumen EXIT
trap 'exit 1' INT TERM HUP

sperre_nehmen() {
  if mkdir "$SPERRE" 2> /dev/null; then
    echo $$ > "$SPERRE/pid"
    SPERRE_GEHALTEN=ja
    return 0
  fi
  alt=$(cat "$SPERRE/pid" 2> /dev/null || true)
  if [ -n "$alt" ] && kill -0 "$alt" 2> /dev/null; then
    return 1
  fi
  if [ -z "$alt" ] && [ -z "$(find "$SPERRE" -maxdepth 0 -mmin +60 2> /dev/null)" ]; then
    # gerade erst angelegt, PID noch nicht geschrieben
    return 1
  fi
  log "Verwaiste Sperre (PID ${alt:-unbekannt}) wird uebernommen"
  rm -rf "$SPERRE"
  mkdir "$SPERRE" 2> /dev/null || return 1
  echo $$ > "$SPERRE/pid"
  SPERRE_GEHALTEN=ja
}

if [ "$MODUS" = lauf ] && ! sperre_nehmen; then
  log "Ein anderer Lauf ist aktiv (PID $(cat "$SPERRE/pid" 2> /dev/null || echo ?)) – nichts zu tun"
  exit 0
fi

ARBEIT=$(mktemp -d)

api() {
  # $1: Pfad unter repos/$GITHUB_REPO/, $2: Zieldatei. Token (falls gesetzt) ueber stdin, nicht in der Prozessliste.
  url="$GITHUB_API/repos/$GITHUB_REPO/$1"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" \
      | curl -fsS -m 30 --retry 2 -K - -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" -o "$2" "$url"
  else
    curl -fsS -m 30 --retry 2 -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" -o "$2" "$url" < /dev/null
  fi
}

ist_sha() {
  # vollstaendige Commit-Kennung (40 Hex-Zeichen), alles andere wird verworfen
  case "$1" in
    *[!0-9a-f]* | "") return 1 ;;
  esac
  [ "${#1}" -eq 40 ]
}

# --- 2. Ziel ermitteln --------------------------------------------------------------------------
api "actions/workflows/$RELEASE_WORKFLOW/runs?branch=$GITHUB_BRANCH&event=push&status=success&per_page=30" \
  "$ARBEIT/release.json" || fehler "GitHub-API nicht erreichbar (Release-Laeufe)"
# Laeufe direkt auf dem Zweig: workflow_dispatch (von Hand, Nachtlauf) und aeltere push-Laeufe. Pull Requests
# mit diesem Zweig als Quelle (dev -> main) pruefen eine Zusammenfuehrung, nicht das Commit selbst.
api "actions/workflows/$CI_WORKFLOW/runs?branch=$GITHUB_BRANCH&status=success&per_page=50" \
  "$ARBEIT/ci_zweig.json" || fehler "GitHub-API nicht erreichbar (CI-Laeufe auf dem Zweig)"
api "actions/workflows/$CI_WORKFLOW/runs?event=merge_group&status=success&per_page=50" \
  "$ARBEIT/ci_queue.json" || fehler "GitHub-API nicht erreichbar (CI-Laeufe der Merge-Queue)"
for antwort in release ci_zweig ci_queue; do
  jq -e 'has("workflow_runs")' "$ARBEIT/$antwort.json" > /dev/null 2>&1 \
    || fehler "Antwort der GitHub-API nicht lesbar ($antwort)"
done

# Gruene CI: in der Merge-Queue des Zweigs (gh-readonly-queue/<zweig>/...) oder direkt auf dem Zweig
{
  jq -r --arg b "$GITHUB_BRANCH" \
    '.workflow_runs[]? | select(.conclusion == "success" and .head_branch == $b)
     | select((.event // "") != "pull_request") | .head_sha' "$ARBEIT/ci_zweig.json"
  jq -r --arg b "$GITHUB_BRANCH" \
    '.workflow_runs[]? | select(.conclusion == "success")
     | select((.head_branch // "") | startswith("gh-readonly-queue/" + $b + "/")) | .head_sha' "$ARBEIT/ci_queue.json"
} | tr -d '\r' > "$ARBEIT/ci_gruen"

jq -r --arg b "$GITHUB_BRANCH" \
  '.workflow_runs[]? | select(.conclusion == "success" and .head_branch == $b) | .head_sha' "$ARBEIT/release.json" \
  | tr -d '\r' > "$ARBEIT/release_gruen"

ZIEL=""
# Release-Laeufe kommen neueste zuerst; das erste Commit, dessen CI ebenfalls gruen ist, gewinnt
while read -r sha; do
  ist_sha "$sha" || continue
  if grep -qx "$sha" "$ARBEIT/ci_gruen"; then
    ZIEL="$sha"
    break
  fi
done < "$ARBEIT/release_gruen"

if [ -z "$ZIEL" ]; then
  log "Kein Stand auf $GITHUB_BRANCH mit erfolgreicher CI und erfolgreichem Release-Lauf gefunden – nichts zu tun"
  exit 0
fi
# Tag wie in release.yml: dev-<Kurzkennung mit 7 Zeichen>
ZIEL_TAG="dev-$(printf '%.7s' "$ZIEL")"

# --- 3. Mit dem laufenden Stand vergleichen ----------------------------------------------------
LAUFEND=$(grep -E '^IMAGE_TAG=' "$STAGING_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"\r ')
LAUFEND_SHA=""
case "$LAUFEND" in
  dev-*)
    kandidat="${LAUFEND#dev-}"
    case "$kandidat" in
      *[!0-9a-f]* | "") ;;
      *) [ "${#kandidat}" -ge 7 ] && LAUFEND_SHA="$kandidat" ;;
    esac
    ;;
esac

if [ "$LAUFEND" = "$ZIEL_TAG" ]; then
  log "Aktuell: $LAUFEND ist der neueste gepruefte Stand"
  exit 0
fi
if [ -n "$LAUFEND_SHA" ]; then
  case "$ZIEL" in
    "$LAUFEND_SHA"*)
      log "Aktuell: $LAUFEND ist der neueste gepruefte Stand"
      exit 0
      ;;
  esac
  # Nicht zurueckspringen, wenn von Hand ein neuerer Stand eingespielt wurde
  if api "compare/$LAUFEND_SHA...$ZIEL?per_page=1" "$ARBEIT/vergleich.json"; then
    richtung=$(jq -r '.status // ""' "$ARBEIT/vergleich.json" | tr -d '\r')
    case "$richtung" in
      behind | identical)
        log "Laufender Stand $LAUFEND ist neuer als $ZIEL_TAG – nichts zu tun"
        exit 0
        ;;
      diverged) log "WARNUNG: $LAUFEND und $ZIEL_TAG liegen auf getrennten Linien; $ZIEL_TAG gilt als massgeblich" ;;
    esac
  else
    log "WARNUNG: Vergleich $LAUFEND mit $ZIEL_TAG nicht moeglich; $ZIEL_TAG gilt als neuer"
  fi
fi

if [ -f "$FEHLGESCHLAGEN" ] && grep -qx "$ZIEL_TAG" "$FEHLGESCHLAGEN"; then
  log "$ZIEL_TAG ist schon einmal gescheitert (Rueckfall auf $LAUFEND); warte auf einen neueren Stand"
  exit 0
fi
PLAN_ZAHL=$(grep -cx "$ZIEL_TAG" "$PLAN_FEHLER" 2> /dev/null || true)
PLAN_ZAHL="${PLAN_ZAHL:-0}"
if [ "$PLAN_ZAHL" -ge "$PLAN_VERSUCHE_MAX" ]; then
  log "$ZIEL_TAG: Vorbereitung ${PLAN_ZAHL}-mal gescheitert; warte auf einen neueren Stand"
  exit 0
fi

if [ -e "$PAUSE" ]; then
  log "Pausiert ($PAUSE vorhanden): bleibt auf $LAUFEND, neuester gepruefter Stand waere $ZIEL_TAG"
  exit 0
fi

if [ "$MODUS" = pruefen ]; then
  log "Pruefen: wuerde $LAUFEND -> $ZIEL_TAG deployen (Commit $ZIEL)"
  exit 0
fi

# --- 4. Deploy ueber den vorhandenen Weg (deploy.sh) --------------------------------------------
deploy() {
  (
    if [ -f "$DEPLOY_ENV" ]; then
      set -a
      # shellcheck disable=SC1090
      . "$DEPLOY_ENV"
      set +a
    fi
    # Das gepruefte Verzeichnis gilt, auch wenn die deploy.env etwas anderes setzt
    MANDARI_DIR="$STAGING_DIR"
    export MANDARI_DIR
    exec sh "$DEPLOY_SCRIPT" "$@"
  )
}

STEMPEL=$(date -u +%Y%m%dT%H%M%SZ)
PROTOKOLL="$PROTOKOLL_DIR/$STEMPEL-$ZIEL_TAG.log"
{
  echo "# Staging-Update $LAUFEND -> $ZIEL_TAG"
  echo "# Commit:  https://github.com/$GITHUB_REPO/commit/$ZIEL"
  echo "# Beginn:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$PROTOKOLL"
log "Deploy $LAUFEND -> $ZIEL_TAG (Commit $ZIEL), Pruefprotokoll $PROTOKOLL"

status_schreiben() {
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LAUFEND" "$ZIEL_TAG" "$1" "$PROTOKOLL" > "$STATUS"
}

echo "## deploy.sh plan $ZIEL_TAG" >> "$PROTOKOLL"
if ! deploy plan "$ZIEL_TAG" >> "$PROTOKOLL" 2>&1; then
  echo "$ZIEL_TAG" >> "$PLAN_FEHLER"
  echo "# Ergebnis: Vorbereitung gescheitert, nichts umgeschaltet" >> "$PROTOKOLL"
  status_schreiben plan-gescheitert
  log "Vorbereitung von $ZIEL_TAG gescheitert (Versuch $((PLAN_ZAHL + 1)) von $PLAN_VERSUCHE_MAX), $LAUFEND bleibt. Protokoll: $PROTOKOLL"
  exit 1
fi

echo "## deploy.sh apply $ZIEL_TAG" >> "$PROTOKOLL"
if deploy apply "$ZIEL_TAG" >> "$PROTOKOLL" 2>&1; then
  ERGEBNIS=ok
else
  ERGEBNIS=gescheitert
  echo "$ZIEL_TAG" >> "$FEHLGESCHLAGEN"
fi
echo "# Ende:    $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$PROTOKOLL"
echo "# Ergebnis: $ERGEBNIS" >> "$PROTOKOLL"
status_schreiben "$ERGEBNIS"

# Alte Pruefprotokolle begrenzen
# shellcheck disable=SC2012
ls -1t "$PROTOKOLL_DIR"/*.log 2> /dev/null | tail -n +$((PROTOKOLLE_BEHALTEN + 1)) | while read -r alt; do
  rm -f "$alt"
done

if [ "$ERGEBNIS" = ok ]; then
  log "Staging laeuft auf $ZIEL_TAG (Pruefungen bestanden). Protokoll: $PROTOKOLL"
  exit 0
fi
log "Deploy von $ZIEL_TAG gescheitert, deploy.sh hat auf den vorherigen Stand zurueckgeschaltet; kein erneuter Versuch fuer dieses Tag. Protokoll: $PROTOKOLL"
exit 1
