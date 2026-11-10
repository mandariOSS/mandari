#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# =============================================================================
# Einzelprüfungen der Wiederherstellung auf einen Zeitpunkt (Issue #1303)
# =============================================================================
# Läuft im Backup-Image, ohne Datenbank und ohne Repository: Zeitangaben und
# Zeitzone, Auswahl der Basissicherung, Aufbewahrung (Basissicherungen und WAL
# zusammen), Lückenprüfung und das Bereitstellen eines angefangenen Segments.
#
#   docker run --rm --entrypoint bash -v "$PWD/tests:/tests:ro" <image> /tests/pitr-einheiten.sh
# =============================================================================
set -uo pipefail

export TZ=Europe/Berlin RESTIC_REPO_1=eins RESTIC_REPO_1_LABEL=eins RESTIC_REPO_2=zwei RESTIC_REPO_2_LABEL=zwei
export BACKUP_PITR=1 BACKUP_PITR_KEEP_DAYS=7

# Eingebunden stellt das Skript nur seine Funktionen bereit
# shellcheck disable=SC1090
. "${MANDARI_BACKUP:-/usr/local/bin/mandari-backup}"

# Die Verzeichnisse des Skripts auf ein Wegwerf-Verzeichnis umlenken
# shellcheck disable=SC2034
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# shellcheck disable=SC2034
STATUS_DIR="$TMP/status"
WORK_DIR="$TMP/work"
PITR_WAL_DIR="$TMP/pitr/wal"
PITR_RESTORE_DIR="$WORK_DIR/pitr-restore"
mkdir -p "$STATUS_DIR" "$WORK_DIR" "$PITR_WAL_DIR"

failed=0
eq() {
    if [ "$2" = "$3" ]; then
        echo "ok     $1"
    else
        echo "FEHLER $1: erhalten [$2], erwartet [$3]"
        failed=1
    fi
}
utc() { date -u -d "$1" +%s; }

echo "--- Zeitangaben (Zeitzone des Containers: $TZ)"
eq "Winterzeit ohne Zusatz" "$(pitr_parse_time '2026-11-10 16:54')" "$(utc '2026-11-10 15:54:00')"
eq "Sommerzeit ohne Zusatz" "$(pitr_parse_time '2026-07-01 12:00:30')" "$(utc '2026-07-01 10:00:30')"
eq "mit T" "$(pitr_parse_time '2026-11-10T16:54:00')" "$(utc '2026-11-10 15:54:00')"
eq "Z" "$(pitr_parse_time '2026-11-10 15:54:00Z')" "$(utc '2026-11-10 15:54:00')"
eq "+01" "$(pitr_parse_time '2026-11-10 16:54:00+01')" "$(utc '2026-11-10 15:54:00')"
eq "+0530" "$(pitr_parse_time '2026-11-10 21:24+0530')" "$(utc '2026-11-10 15:54:00')"
eq "-03:30" "$(pitr_parse_time '2026-11-10 12:24:00-03:30')" "$(utc '2026-11-10 15:54:00')"
eq "führende Nullen sind keine Oktalzahlen" "$(pitr_parse_time '2026-08-09 08:09:09+08:09')" \
    "$(( $(utc '2026-08-09 08:09:09') - 8 * 3600 - 9 * 60 ))"
for bad in '2026-02-31 10:00' '2026-03-29 02:30' 'gestern' '2026-11-10' '2026-11-10 16:54; id' '2026-11-10 16:54 CET'; do
    pitr_parse_time "$bad" >/dev/null
    eq "abgelehnt: $bad" "$?" 1
done

echo "--- Aufbewahrung als Rechnung"
day=86400
now=$(date +%s)
window=$((now - 7 * day))
list="a $((now - 9 * day)) k1 000000010000000000000010 gz /pitr/basis/a
b $((now - 8 * day)) k1 000000010000000000000020 gz /pitr/basis/b
c $((now - 6 * day)) k2 000000010000000000000005 gz /pitr/basis/c
d $((now - 1 * day)) k2 000000010000000000000030 gz /pitr/basis/d"
verdicts() { pitr_plan "$1" | awk '{printf "%s:%s ", $1, $2}'; }
eq "Anker ist die jüngste Basissicherung vor dem Fenster" "$(verdicts "$window" <<< "$list")" \
    "loeschen:a anker:b behalten:c behalten:d "
eq "alles im Fenster" "$(verdicts "$((now - 20 * day))" <<< "$list")" "behalten:a behalten:b behalten:c behalten:d "
eq "Fenster null: nur die neueste bleibt" "$(verdicts "$now" <<< "$list")" "loeschen:a loeschen:b loeschen:c anker:d "
eq "ohne Basissicherungen" "$(verdicts "$window" <<< "")" ""

echo "--- Aufbewahrung gegen zwei Repositorys (restic ersetzt)"
snapshot() {
    printf '{"id":"%s","time":"2026-01-01T00:00:00Z","hostname":"x-pitr","paths":["/pitr/basis/%s"],' "$1" "$1"
    printf '"tags":["pitr-basis","kette=%s","wal=%s","ende=%s","packung=gz"]}' "$3" "$4" "$2"
}
# Repository 1 hält alle vier, Repository 2 nur b und d; ein Stand ohne Angaben wird übergangen
{
    printf '['
    snapshot a "$((now - 9 * day))" k1 000000010000000000000010
    printf ','
    snapshot b "$((now - 8 * day))" k1 000000010000000000000020
    printf ','
    snapshot c "$((now - 6 * day))" k2 000000010000000000000005
    printf ','
    snapshot d "$((now - 1 * day))" k2 000000010000000000000030
    printf ',{"id":"fremd","time":"2026-01-01T00:00:00Z","paths":["/x"],"tags":["pitr-basis"]}]'
} > "$TMP/repo1.json"
{
    printf '['
    snapshot b "$((now - 8 * day))" k1 000000010000000000000020
    printf ','
    snapshot d "$((now - 1 * day))" k2 000000010000000000000030
    printf ']'
} > "$TMP/repo2.json"
repo() {
    local n="$1"
    shift
    echo "$n $*" >> "$TMP/aufrufe"
    case "$*" in
        *snapshots*pitr-basis*)
            [ -f "$TMP/repo$n.json" ] || return 1
            cat "$TMP/repo$n.json"
            ;;
    esac
}
fill_wal() {
    rm -rf "$PITR_WAL_DIR"
    mkdir -p "$PITR_WAL_DIR/k0" "$PITR_WAL_DIR/k1" "$PITR_WAL_DIR/k2"
    echo k2 > "$PITR_WAL_DIR/aktuell"
    : > "$PITR_WAL_DIR/k0/000000010000000000000001.gz"
    local seg
    for seg in 1E 1F 20 21; do : > "$PITR_WAL_DIR/k1/0000000100000000000000$seg.gz"; done
    : > "$PITR_WAL_DIR/k1/00000002.history.gz"
    for seg in 04 05 30 31; do : > "$PITR_WAL_DIR/k2/0000000100000000000000$seg.gz"; done
    : > "$PITR_WAL_DIR/k2/000000010000000000000032.gz.partial"
}
wal_files() { (cd "$PITR_WAL_DIR" && find k* -type f | sort | tr '\n' ' '); }

eq "Liste der Basissicherungen" "$(pitr_base_list 1 | awk '{printf "%s:%s:%s ", $1, $3, $6}')" \
    "a:k1:/pitr/basis/a b:k1:/pitr/basis/b c:k2:/pitr/basis/c d:k2:/pitr/basis/d "
eq "Auswahl: jüngste vor dem Zeitpunkt" "$(pitr_pick_base 1 "$((now - 5 * day))" | cut -d' ' -f1)" c
eq "Auswahl: genau am Ende der Basissicherung" "$(pitr_pick_base 1 "$((now - 6 * day))" | cut -d' ' -f1)" c
eq "Auswahl: ohne Zeitpunkt die neueste" "$(pitr_pick_base 1 "" | cut -d' ' -f1)" d
eq "Auswahl: anderes Repository" "$(pitr_pick_base 2 "$((now - 5 * day))" | cut -d' ' -f1)" b
pitr_pick_base 1 "$((now - 10 * day))" >/dev/null
eq "Auswahl: vor der ältesten Basissicherung gibt es nichts" "$?" 1

fill_wal
: > "$TMP/aufrufe"
pitr_retention >/dev/null
eq "Aufräumen meldet Erfolg" "$?" 0
eq "nur a wird entfernt, nur im Repository 1" "$(grep -c ' forget .*--quiet a$' "$TMP/aufrufe") $(grep -c ' forget --quiet [a-d]' "$TMP/aufrufe")" "1 1"
eq "WAL-Stände je Repository nach Regel" "$(grep -c 'forget --quiet --host .*-pitr --tag pitr-wal --keep-last 1 --keep-within-hourly 2d' "$TMP/aufrufe")" 2
# Der älteste Anker ist b (Kette k1 ab Segment 20): Davor darf weg, alles danach bleibt
eq "lokales WAL: nichts fehlt ab dem ältesten Anker" "$(wal_files)" \
    "k1/000000010000000000000020.gz k1/000000010000000000000021.gz k1/00000002.history.gz k2/000000010000000000000004.gz k2/000000010000000000000005.gz k2/000000010000000000000030.gz k2/000000010000000000000031.gz k2/000000010000000000000032.gz.partial "

fill_wal
before=$(wal_files)
mv "$TMP/repo2.json" "$TMP/repo2.weg"
pitr_retention >/dev/null
eq "Repository nicht lesbar: Fehler" "$?" 1
eq "Repository nicht lesbar: lokales WAL unberührt" "$(wal_files)" "$before"
echo '[]' > "$TMP/repo1.json"
echo 'null' > "$TMP/repo2.json"
pitr_retention >/dev/null
eq "ohne Basissicherung: kein Fehler" "$?" 0
eq "ohne Basissicherung: lokales WAL unberührt" "$(wal_files)" "$before"

echo "--- Lücken im WAL"
gap="$TMP/luecke"
mkdir -p "$gap"
for seg in 0000000100000000000000FE 0000000100000000000000FF 000000010000000100000000 000000010000000100000001; do
    : > "$gap/$seg.gz"
done
: > "$gap/00000002.history.gz"
: > "$gap/000000010000000100000002.gz.partial"
eq "lückenlos über die Grenze FF/00" "$(pitr_wal_gap "$gap" 16777216)" ""
rm "$gap/000000010000000100000000.gz"
eq "Lücke wird benannt" "$(pitr_wal_gap "$gap" 16777216)" "0000000100000000000000FF"

echo "--- WAL bereitstellen, angefangenes Segment"
src="$TMP/quelle"
mkdir -p "$src" "$PITR_RESTORE_DIR/wal"
echo "7000000000000000001 1048576" > "$src/.kennung"
for seg in 000000010000000000000010 000000010000000000000011; do
    head -c 1000 /dev/urandom | gzip > "$src/$seg.gz"
done
# Ein gzip-Strom ohne Ende, wie ihn ein laufender oder abgebrochener Empfänger hinterlässt:
# Was vollständig ankam, muss zurückkommen.
head -c 300000 /dev/urandom > "$TMP/roh"
gzip -c "$TMP/roh" | head -c 290000 > "$src/000000010000000000000012.gz.partial"
pitr_stage_wal "$src" 000000010000000000000011 cp >/dev/null
eq "bereitgestellt ab dem ersten nötigen Segment" "$(cd "$PITR_RESTORE_DIR/wal" && echo *)" \
    "000000010000000000000011.gz 000000010000000000000012"
eq "angefangenes Segment auf Segmentgröße aufgefüllt" "$(stat -c %s "$PITR_RESTORE_DIR/wal/000000010000000000000012")" 1048576
eq "sein Anfang ist unverändert" "$(head -c 200000 "$PITR_RESTORE_DIR/wal/000000010000000000000012" | sha256sum)" \
    "$(head -c 200000 "$TMP/roh" | sha256sum)"
eq "Segmentgröße aus der Kennung" "$(pitr_segsize "$src")" 1048576
eq "Segmentgröße ohne Kennung" "$(pitr_segsize "$TMP/fehlt")" 16777216

if [ "$failed" -ne 0 ]; then
    echo "Einzelprüfungen fehlgeschlagen"
    exit 1
fi
echo "Alle Einzelprüfungen bestanden"
