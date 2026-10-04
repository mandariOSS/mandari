#!/bin/bash
# =============================================================================
# Mandari - Backup Script
# =============================================================================
# Sichert Datenbanken, hochgeladene Dateien, Dokument-Cache und – nur verschlüsselt –
# die Konfiguration (.env) in ein Archiv und stellt daraus wieder her.
#
# Usage:
#   ./backup.sh                    # Create backup in ./backups/
#   ./backup.sh /path/to/backups   # Create backup in custom directory
#   ./backup.sh --no-files         # Ohne Dokument-Cache (sehr große Ablagen)
#   ./backup.sh --restore FILE     # Restore from backup file
#   ./backup.sh --quiet            # Only output warnings and errors (for cron)
#   ./backup.sh --verify           # Verify backup integrity after creation
#   ./backup.sh --help             # Alle Optionen und Umgebungsvariablen
#
# Die .env landet nur verschlüsselt im Archiv (Passphrase aus BACKUP_PASSPHRASE oder
# BACKUP_PASSPHRASE_FILE), sonst gar nicht. Anleitung: DEPLOYMENT.md, „Sicherung“.
# =============================================================================

set -euo pipefail
# Archive enthalten personenbezogene Daten: nur für den Eigentümer lesbar anlegen
umask 077

# Container-Namen folgen dem Compose-Projektnamen (siehe docker-compose.yml)
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-mandari}"
export COMPOSE_PROJECT_NAME
APP_CONTAINER="${COMPOSE_PROJECT_NAME}"
DB_CONTAINER="${COMPOSE_PROJECT_NAME}-postgres"
WEBSITE_CONTAINER="${COMPOSE_PROJECT_NAME}-website"

# Compose-Dienst der Anwendung und die Pfade seiner Datei-Volumes (docker-compose.yml).
# Gesichert wird über "docker compose run" auf diesen Dienst: Damit gelten dieselben
# Volumes bzw. Bind-Mounts wie im Betrieb, auch aus Override-Dateien.
APP_SERVICE="mandari"
MEDIA_PATH="/app/media"   # Uploads (Volume mandari_media)
FILES_PATH="/app/files"   # Dokument-Cache der OParl-Dateien (Volume mandari_files)

# Verschlüsselung der Konfiguration im Archiv. Die Parameter stehen zusätzlich in
# metadata.json; ältere Archive bleiben nur mit ihren damaligen Parametern lesbar.
CONFIG_CIPHER_ARGS=(-aes-256-cbc -pbkdf2 -iter 600000 -md sha256)
CONFIG_CIPHER_LABEL="openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256"
DEFAULT_PASSPHRASE_FILE=".backup-passphrase"
MIN_PASSPHRASE_LENGTH=16
# Ab dieser Größe des Dokument-Cache-Anteils (KB) folgt ein Hinweis auf --no-files
FILES_SIZE_HINT_KB=$((5 * 1024 * 1024))
# So viele Archive bleiben im Zielverzeichnis
KEEP_ARCHIVES=7


# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="mandari_backup_${TIMESTAMP}"

# Flags
QUIET=false
VERIFY=false
BACKUP_DIR="./backups"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Log file
mkdir -p "$SCRIPT_DIR/logs"
BACKUP_LOG="$SCRIPT_DIR/logs/backup.log"

# Temporäre Verzeichnisse, die beim Beenden entfernt werden (auch bei Abbruch:
# Arbeitsverzeichnisse enthalten unverschlüsselte Datenbank-Dumps)
CLEANUP_PATHS=()
cleanup() {
    local path
    for path in "${CLEANUP_PATHS[@]+"${CLEANUP_PATHS[@]}"}"; do
        rm -rf -- "$path"
    done
}
trap cleanup EXIT

# =============================================================================
# Helper Functions
# =============================================================================
log() {
    if [ "$QUIET" = false ]; then
        echo -e "${GREEN}[BACKUP]${NC} $1"
    fi
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1" >&2
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
    exit 1
}

info() {
    if [ "$QUIET" = false ]; then
        echo -e "${BLUE}[INFO]${NC} $1"
    fi
}

is_true() {
    case "${1,,}" in
        true|1|yes|ja) return 0 ;;
    esac
    return 1
}

# Sicheres Lesen einzelner Werte aus .env (kein source = keine Code-Injection)
# Usage: get_env_var KEY [DEFAULT] [DATEI]
get_env_var() {
    local key="$1"
    local default="${2:-}"
    local file="${3:-.env}"
    local val
    val=$(grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d'=' -f2-) || true
    echo "${val:-$default}"
}

# Einstellung aus der Umgebung, sonst aus .env
setting() {
    local name="$1"
    if [ -n "${!name:-}" ]; then
        printf '%s' "${!name}"
    else
        get_env_var "$name"
    fi
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

human_size() {
    du -h "$1" 2>/dev/null | cut -f1
}

# Run a command with spinner, output goes to log file
# Usage: run_step "Description" command arg1 arg2 ...
run_step() {
    local description="$1"
    shift

    if [ "$QUIET" = true ]; then
        # Im Quiet-Modus: direkt ausführen, nur Fehler ausgeben
        local rc=0
        "$@" >> "$BACKUP_LOG" 2>&1 || rc=$?
        return $rc
    fi

    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    # Start command in background, output to log
    "$@" >> "$BACKUP_LOG" 2>&1 &
    local pid=$!

    # Show spinner
    printf "  %-30s " "$description"
    while kill -0 "$pid" 2>/dev/null; do
        printf "\b${spin_chars:$i:1}"
        i=$(( (i + 1) % ${#spin_chars} ))
        sleep 0.1
    done

    # Check exit code
    local exit_code=0
    wait "$pid" || exit_code=$?

    if [ $exit_code -eq 0 ]; then
        printf "\b${GREEN}✓${NC}\n"
    else
        printf "\b${RED}✗${NC}\n"
        warn "Fehlgeschlagen! Details: cat $BACKUP_LOG"
    fi

    return $exit_code
}

# Run a command that writes to a specific file (stdout → file, stderr → log)
# Usage: run_step_to_file "Description" output_file command arg1 ...
run_step_to_file() {
    local description="$1"
    local output_file="$2"
    shift 2

    if [ "$QUIET" = true ]; then
        local rc=0
        "$@" > "$output_file" 2>> "$BACKUP_LOG" || rc=$?
        return $rc
    fi

    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    # Start command in background, stdout → file, stderr → log
    "$@" > "$output_file" 2>> "$BACKUP_LOG" &
    local pid=$!

    # Show spinner
    printf "  %-30s " "$description"
    while kill -0 "$pid" 2>/dev/null; do
        printf "\b${spin_chars:$i:1}"
        i=$(( (i + 1) % ${#spin_chars} ))
        sleep 0.1
    done

    # Check exit code
    local exit_code=0
    wait "$pid" || exit_code=$?

    if [ $exit_code -eq 0 ]; then
        printf "\b${GREEN}✓${NC}\n"
    else
        printf "\b${RED}✗${NC}\n"
        warn "Fehlgeschlagen! Details: cat $BACKUP_LOG"
    fi

    return $exit_code
}

# Warte bis Container healthy ist (mit Spinner + Go-Template-Fix)
wait_for_healthy() {
    local container=$1
    local max_attempts=${2:-30}
    local attempt=0
    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    while [ $attempt -lt $max_attempts ]; do
        local status
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "unknown")

        if [ "$status" = "healthy" ]; then
            return 0
        fi

        if [ "$status" = "unhealthy" ]; then
            return 1
        fi

        if [ "$status" = "no-healthcheck" ]; then
            local running
            running=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "")
            if [ "$running" = "running" ]; then
                return 0
            fi
        fi

        attempt=$((attempt + 1))
        if [ "$QUIET" = false ]; then
            printf "\b${spin_chars:$i:1}"
            i=$(( (i + 1) % ${#spin_chars} ))
        fi
        sleep 2
    done

    return 1
}

# Verifikation aller Container (wie install.sh)
verify_installation() {
    log "Verifikation..."
    echo ""

    local entry container label
    for entry in \
        "${COMPOSE_PROJECT_NAME}-postgres:PostgreSQL" \
        "${COMPOSE_PROJECT_NAME}-redis:Redis" \
        "${COMPOSE_PROJECT_NAME}-elasticsearch:Elasticsearch" \
        "${COMPOSE_PROJECT_NAME}:Mandari" \
        "${COMPOSE_PROJECT_NAME}-website:Website" \
        "${COMPOSE_PROJECT_NAME}-caddy:Caddy" \
        "${COMPOSE_PROJECT_NAME}-ingestor:Ingestor" \
        "${COMPOSE_PROJECT_NAME}-worker:Worker" \
        "${COMPOSE_PROJECT_NAME}-worker-heavy:Worker OCR/KI"; do
        container="${entry%%:*}"
        label="${entry#*:}"
        local status
        local health
        # docker inspect schreibt bei unbekanntem Container eine Leerzeile – nicht in die Anzeige übernehmen
        status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null) || status="missing"
        health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null) || health="no-healthcheck"

        printf "  %-14s " "$label"
        if [ "$status" = "running" ]; then
            if [ "$health" = "healthy" ]; then
                echo -e "${GREEN}✓ healthy${NC}"
            elif [ "$health" = "no-healthcheck" ] || [ -z "$health" ] || [ "$health" = "none" ]; then
                echo -e "${GREEN}✓ running${NC}"
            elif [ "$health" = "starting" ]; then
                echo -e "${YELLOW}⏳ starting${NC}"
            else
                echo -e "${YELLOW}⚠ $health${NC}"
            fi
        else
            echo -e "${RED}✗ $status${NC}"
        fi
    done
    echo ""
}

# -----------------------------------------------------------------------------
# Datei-Volumes der Anwendung
# -----------------------------------------------------------------------------
# Kurzlebiger Container des Anwendungsdienstes: ohne abhängige Dienste, als root (liest
# und schreibt unabhängig vom Eigentümer der Dateien), ohne Autoheal-Label (sein
# Healthcheck schlägt fehl, deploy/scripts/restart-unhealthy.sh soll ihn nicht anfassen).
volume_container() {
    local suffix="$1"
    shift
    docker compose run --rm --no-deps -T --user root \
        --label mandari.autoheal=false \
        --name "${COMPOSE_PROJECT_NAME}-sicherung-${suffix}-$$" \
        "$@"
}

# Inhalt eines Pfads im Container als tar-Strom auf stdout. GNU tar meldet mit Exit-Code 1,
# dass sich Dateien während des Lesens geändert haben (laufender Betrieb) – das ist hier
# kein Fehler.
volume_tar_create() {
    local path="$1"
    # shellcheck disable=SC2016 # Das Skript läuft im Container, $1 ist dort der Pfad
    volume_container "${path##*/}" --entrypoint sh "$APP_SERVICE" -c \
        'tar -C "$1" -cf - --numeric-owner . ; rc=$?; [ "$rc" -eq 1 ] && rc=0; exit "$rc"' \
        volume-backup "$path" < /dev/null
}

# tar-Strom von stdin in einen Pfad im Container auspacken (vorhandene Dateien gleichen
# Namens werden überschrieben, andere bleiben liegen)
volume_tar_extract() {
    local path="$1"
    volume_container "${path##*/}" --entrypoint tar "$APP_SERVICE" -C "$path" -xf - --numeric-owner
}

# -----------------------------------------------------------------------------
# Konfiguration verschlüsseln
# -----------------------------------------------------------------------------
PASSPHRASE=""
PASSPHRASE_ORIGIN=""

# Passphrase aus BACKUP_PASSPHRASE (nur Umgebung) oder aus der Datei BACKUP_PASSPHRASE_FILE
# (Umgebung oder .env; Vorgabe ./.backup-passphrase, falls vorhanden). Rückgabe 1 nur bei
# einer angegebenen, aber unbrauchbaren Datei; ohne Angabe bleibt PASSPHRASE leer.
load_passphrase() {
    PASSPHRASE=""
    PASSPHRASE_ORIGIN=""
    if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
        PASSPHRASE="$BACKUP_PASSPHRASE"
        PASSPHRASE_ORIGIN="BACKUP_PASSPHRASE"
        return 0
    fi

    local file
    file="$(setting BACKUP_PASSPHRASE_FILE)"
    if [ -z "$file" ]; then
        [ -f "$DEFAULT_PASSPHRASE_FILE" ] || return 0
        file="$DEFAULT_PASSPHRASE_FILE"
    fi
    if [ ! -r "$file" ]; then
        warn "Passphrase-Datei nicht lesbar: $file"
        return 1
    fi
    PASSPHRASE="$(head -n 1 "$file" | tr -d '\r\n')"
    PASSPHRASE_ORIGIN="$file"
    if [ -z "$PASSPHRASE" ]; then
        warn "Passphrase-Datei ist leer: $file"
        return 1
    fi
    if [ -n "$(find "$file" -maxdepth 0 -perm /077 2>/dev/null)" ]; then
        warn "Passphrase-Datei $file ist für andere lesbar – bitte: chmod 600 \"$file\""
    fi
    return 0
}

# Die Passphrase geht nur über die Umgebung des openssl-Prozesses, nie über die
# Kommandozeile (Prozessliste) oder eine Datei.
encrypt_config() {
    MANDARI_BACKUP_PASSPHRASE="$PASSPHRASE" openssl enc "${CONFIG_CIPHER_ARGS[@]}" -salt \
        -pass env:MANDARI_BACKUP_PASSPHRASE -in "$1" -out "$2"
}

# Entschlüsselt nach stdout
decrypt_config() {
    MANDARI_BACKUP_PASSPHRASE="$PASSPHRASE" openssl enc -d "${CONFIG_CIPHER_ARGS[@]}" \
        -pass env:MANDARI_BACKUP_PASSPHRASE -in "$1"
}

# -----------------------------------------------------------------------------
# Archiv lesen (Wiederherstellung und Prüfung)
# -----------------------------------------------------------------------------
ARCHIVE=""
ARCHIVE_ROOT=""
ARCHIVE_LISTING=""

archive_list() {
    tar -tzf "$1" > "$2"
}

archive_has() {
    grep -qxF "$ARCHIVE_ROOT/$1" "$ARCHIVE_LISTING"
}

# Einen Bestandteil des Archivs nach stdout, ohne ihn auf die Platte zu schreiben
archive_cat() {
    tar --occurrence=1 -xzOf "$ARCHIVE" "$ARCHIVE_ROOT/$1"
}

# -----------------------------------------------------------------------------
# Datenbank wiederherstellen
# -----------------------------------------------------------------------------
psql_admin() {
    docker exec -i "$DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$1" -d postgres "${@:2}"
}

# Wartet, bis PostgreSQL über TCP antwortet. Während der Erstinitialisierung eines neuen
# Volumes läuft nur ein Hilfsserver ohne TCP; der Healthcheck (Socket) meldet ihn schon.
wait_for_database() {
    local user="$1"
    local attempt
    for attempt in $(seq 1 60); do
        if docker exec "$DB_CONTAINER" pg_isready -q -h 127.0.0.1 -U "$user" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# Passwort der Datenbankrolle an die (wiederhergestellte) .env angleichen. Das Passwort geht
# über die Umgebung und \getenv an psql – nicht über Kommandozeile oder Protokoll.
sync_db_password() {
    local user
    user=$(get_env_var POSTGRES_USER mandari)
    MANDARI_DB_PASSWORD="$(get_env_var POSTGRES_PASSWORD)" \
        docker exec -i -e MANDARI_DB_PASSWORD "$DB_CONTAINER" \
        psql -X -q -v ON_ERROR_STOP=1 -U "$user" -d postgres -v rolle="$user" <<'SQL'
\getenv neues_passwort MANDARI_DB_PASSWORD
ALTER ROLE :"rolle" WITH PASSWORD :'neues_passwort';
SQL
}

# Dump in eine Zwischen-Datenbank einspielen und erst danach gegen die bestehende tauschen.
# Schlägt das Einspielen fehl, bleibt die bisherige Datenbank unverändert.
# Usage: restore_database ARCHIVTEIL DATENBANK EIGENTUEMER
restore_database() {
    local part="$1"
    local db="$2"
    local owner="$3"
    local tmp="${db}_restore"

    printf '%s\n' \
        'DROP DATABASE IF EXISTS :"ziel";' \
        'CREATE DATABASE :"ziel" OWNER :"eigentuemer";' |
        psql_admin "$owner" -v ziel="$tmp" -v eigentuemer="$owner" || return 1

    if ! archive_cat "$part" |
        docker exec -i "$DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$owner" -d "$tmp"; then
        printf '%s\n' 'DROP DATABASE IF EXISTS :"ziel";' | psql_admin "$owner" -v ziel="$tmp" || true
        return 1
    fi

    printf '%s\n' \
        'DROP DATABASE IF EXISTS :"db" WITH (FORCE);' \
        'ALTER DATABASE :"ziel" RENAME TO :"db";' |
        psql_admin "$owner" -v ziel="$tmp" -v db="$db"
}

restore_volume() {
    archive_cat "$1" | volume_tar_extract "$2"
}

show_usage() {
    cat << 'EOF'
Verwendung: ./backup.sh [VERZEICHNIS] [OPTIONEN]
            ./backup.sh --restore DATEI

Sichert die Datenbanken (mandari und Website), die hochgeladenen Dateien, den
Dokument-Cache und – verschlüsselt – die Konfiguration (.env) als ein Archiv.

Optionen:
  VERZEICHNIS      Zielverzeichnis (Vorgabe: ./backups/); die letzten 7 Archive bleiben
  --no-media       Hochgeladene Dateien (/app/media) nicht sichern
  --no-files       Dokument-Cache (/app/files) nicht sichern – lässt sich aus den
                   Ratsinformationssystemen neu laden; für sehr große Ablagen
  --verify         Archiv nach dem Erstellen prüfen
  --quiet          Nur Warnungen und Fehler ausgeben (für Cron)
  --restore DATEI  Aus einem Archiv wiederherstellen (ersetzt die aktuellen Daten)
  -h, --help       Diese Hilfe anzeigen

Umgebungsvariablen (BACKUP_NO_MEDIA, BACKUP_NO_FILES und BACKUP_PASSPHRASE_FILE auch als
Zeile in .env):
  BACKUP_PASSPHRASE       Passphrase für die Konfiguration im Archiv (nur Umgebung)
  BACKUP_PASSPHRASE_FILE  Datei mit der Passphrase in der ersten Zeile
                          (Vorgabe: ./.backup-passphrase, falls vorhanden)
  BACKUP_NO_MEDIA=true    wie --no-media
  BACKUP_NO_FILES=true    wie --no-files
  S3_BACKUP_BUCKET        Archiv zusätzlich nach s3://<bucket>/mandari/ hochladen

Ohne Passphrase wird die Konfiguration nicht gesichert (mit Warnung). Die Passphrase wird
auch für die Wiederherstellung gebraucht und gehört zusätzlich außerhalb des Servers
verwahrt.
EOF
}

# =============================================================================
# Parse Arguments
# =============================================================================
RESTORE_MODE=false
RESTORE_FILE=""
NO_MEDIA_FLAG=false
NO_FILES_FLAG=false

# Zuerst Flags parsen
args=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --restore)
            RESTORE_MODE=true
            RESTORE_FILE="${2:-}"
            shift 2 || shift
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        --verify)
            VERIFY=true
            shift
            ;;
        --no-media)
            NO_MEDIA_FLAG=true
            shift
            ;;
        --no-files)
            NO_FILES_FLAG=true
            shift
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        -*)
            error "Unbekannte Option: $1 (siehe $0 --help)"
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

# Backup-Verzeichnis aus positionalen Argumenten
if [ ${#args[@]} -gt 0 ]; then
    BACKUP_DIR="${args[0]}"
fi

# Log-Datei initialisieren
echo "=== Mandari Backup $(date) ===" > "$BACKUP_LOG"

# =============================================================================
# Restore Mode
# =============================================================================
if [ "$RESTORE_MODE" = true ]; then
    if [ -z "$RESTORE_FILE" ]; then
        error "Usage: $0 --restore BACKUP_FILE"
    fi
    if [ ! -f "$RESTORE_FILE" ]; then
        error "Backup-Datei nicht gefunden: $RESTORE_FILE"
    fi

    log "Wiederherstellung vorbereiten..."

    RESTORE_DIR=$(mktemp -d)
    CLEANUP_PATHS+=("$RESTORE_DIR")
    ARCHIVE="$RESTORE_FILE"
    ARCHIVE_LISTING="$RESTORE_DIR/inhalt.txt"

    # 1. Archiv vollständig lesen (prüft zugleich die Prüfsumme) – noch nichts verändern
    if ! run_step "Archiv prüfen" archive_list "$ARCHIVE" "$ARCHIVE_LISTING"; then
        error "Archiv ist beschädigt oder kein tar.gz: $RESTORE_FILE"
    fi
    ARCHIVE_ROOT=$(head -n 1 "$ARCHIVE_LISTING" | cut -d/ -f1)
    if [ -z "$ARCHIVE_ROOT" ] || ! archive_has postgres.sql; then
        error "Keine mandari-Sicherung (postgres.sql fehlt): $RESTORE_FILE"
    fi

    # 2. Kleine Bestandteile auspacken (Konfiguration, Metadaten)
    small_parts=()
    for part in metadata.json config.env.enc .env; do
        if archive_has "$part"; then
            small_parts+=("$ARCHIVE_ROOT/$part")
        fi
    done
    if [ ${#small_parts[@]} -gt 0 ]; then
        tar --occurrence=1 -xzf "$ARCHIVE" -C "$RESTORE_DIR" "${small_parts[@]}" 2>> "$BACKUP_LOG" ||
            error "Bestandteile ließen sich nicht auspacken (Details: $BACKUP_LOG)"
    fi
    CONTENT="$RESTORE_DIR/$ARCHIVE_ROOT"

    # 3. Konfiguration bestimmen – vor jeder Änderung, damit Fehler nichts zerstören
    RESTORED_ENV=""
    CONFIG_NOTE=""
    if archive_has config.env.enc; then
        load_passphrase || error "Passphrase nicht lesbar – Abbruch, nichts verändert."
        if [ -n "$PASSPHRASE" ]; then
            RESTORED_ENV="$RESTORE_DIR/env.restored"
            if ! decrypt_config "$CONTENT/config.env.enc" > "$RESTORED_ENV" 2>> "$BACKUP_LOG" ||
                ! grep -q '^SECRET_KEY=' "$RESTORED_ENV"; then
                error "Die Konfiguration ließ sich nicht entschlüsseln (Passphrase aus $PASSPHRASE_ORIGIN falsch?). Abbruch, nichts verändert."
            fi
            CONFIG_NOTE="aus der Sicherung (entschlüsselt)"
        elif [ -f .env ]; then
            CONFIG_NOTE="vorhandene .env bleibt (Sicherung verschlüsselt, keine Passphrase angegeben)"
        else
            error "Die Konfiguration liegt verschlüsselt im Archiv, und hier gibt es keine .env.
  Passphrase angeben: BACKUP_PASSPHRASE=… $0 --restore $RESTORE_FILE
  (oder BACKUP_PASSPHRASE_FILE=/pfad/zur/datei)"
        fi
    elif archive_has .env; then
        # Sicherungen älterer Versionen enthalten die .env unverschlüsselt
        RESTORED_ENV="$CONTENT/.env"
        CONFIG_NOTE="aus der Sicherung (ältere Sicherung, unverschlüsselt)"
        warn "Diese ältere Sicherung enthält die Konfiguration im Klartext – Archiv sicher verwahren oder löschen."
    elif [ -f .env ]; then
        CONFIG_NOTE="vorhandene .env bleibt (Sicherung ohne Konfiguration)"
    else
        error "Die Sicherung enthält keine Konfiguration, und hier gibt es keine .env.
  Die .env der gesicherten Installation (mit demselben ENCRYPTION_MASTER_KEY) hierher kopieren
  und die Wiederherstellung erneut starten."
    fi

    if [ -z "$RESTORED_ENV" ]; then
        warn "Die vorhandene .env wird verwendet. Verschlüsselte Inhalte sind nur lesbar, wenn ihr"
        warn "ENCRYPTION_MASTER_KEY dem der gesicherten Installation entspricht."
    elif [ -f .env ]; then
        current_user=$(get_env_var POSTGRES_USER mandari)
        restored_user=$(get_env_var POSTGRES_USER mandari "$RESTORED_ENV")
        if [ "$current_user" != "$restored_user" ]; then
            error "Die Sicherung nutzt den Datenbankbenutzer $restored_user, diese Installation $current_user.
  Das lässt sich nicht automatisch übertragen. Abbruch, nichts verändert."
        fi
    fi

    # 4. Übersicht und Bestätigung
    META="$CONTENT/metadata.json"
    meta_value() {
        if [ -f "$META" ]; then
            sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$META" | head -n 1
        fi
    }
    part_state() {
        if archive_has "$1"; then echo "ja"; else echo "nicht enthalten – bleibt unverändert"; fi
    }
    echo ""
    echo "  Sicherung:        $RESTORE_FILE"
    echo "  Erstellt:         $(meta_value timestamp)"
    echo "  Version:          $(meta_value mandari_version)"
    echo "  Mandari-DB:       ja"
    echo "  Website-DB:       $(part_state postgres_website.sql)"
    echo "  Uploads:          $(part_state media.tar)"
    echo "  Dokument-Cache:   $(part_state files.tar)"
    echo "  Konfiguration:    $CONFIG_NOTE"
    echo ""
    warn "ACHTUNG: Die Datenbanken werden durch den Stand der Sicherung ersetzt, Dateien überschrieben."
    warn "Die Dienste sind währenddessen gestoppt. Vorher ggf. den aktuellen Stand sichern: ./backup.sh"
    read -r -p "Wiederherstellen aus $RESTORE_FILE? [j/N]: " confirm
    if [[ ! "$confirm" =~ ^[JjYy]$ ]]; then
        log "Wiederherstellung abgebrochen"
        exit 0
    fi

    RESTORE_PROBLEMS=()
    PREVIOUS_ENV=""
    OLD_DB_PASSWORD=""

    # 5. Dienste stoppen (ohne .env gibt es nichts zu stoppen)
    if [ -f .env ]; then
        run_step "Dienste stoppen" docker compose down ||
            error "Dienste ließen sich nicht stoppen – Abbruch, nichts verändert."
    fi

    # 6. Konfiguration einsetzen; die bisherige bleibt als Datei liegen
    if [ -n "$RESTORED_ENV" ]; then
        if [ -f .env ] && ! cmp -s "$RESTORED_ENV" .env; then
            PREVIOUS_ENV=".env.vor-wiederherstellung-$TIMESTAMP"
            cp .env "$PREVIOUS_ENV"
            OLD_DB_PASSWORD=$(get_env_var POSTGRES_PASSWORD)
            info "Bisherige Konfiguration aufgehoben: $PREVIOUS_ENV"
        fi
        cp "$RESTORED_ENV" .env
        chmod 600 .env
        log "Konfiguration wiederhergestellt"
    fi

    DB_USER=$(get_env_var POSTGRES_USER mandari)
    DB_NAME=$(get_env_var POSTGRES_DB mandari)
    WEBSITE_DB=$(get_env_var WEBSITE_DB mandari_website)
    ABORT_HINT="Die Dienste sind gestoppt."
    if [ -n "$PREVIOUS_ENV" ]; then
        ABORT_HINT="$ABORT_HINT Bisherige Konfiguration: $PREVIOUS_ENV (zurückkopieren nach .env, dann docker compose up -d)."
    fi

    # 7. Nur PostgreSQL starten
    log "Datenbank starten..."
    docker compose up -d postgres >> "$BACKUP_LOG" 2>&1 ||
        error "PostgreSQL ließ sich nicht starten (Details: $BACKUP_LOG). $ABORT_HINT"
    printf "  %-30s " "PostgreSQL"
    if wait_for_healthy "$DB_CONTAINER" 30 && wait_for_database "$DB_USER"; then
        printf "\b${GREEN}✓ bereit${NC}\n"
    else
        printf "\b${RED}✗${NC}\n"
        error "PostgreSQL ist nicht bereit (Details: docker logs $DB_CONTAINER). $ABORT_HINT"
    fi

    # Neue Konfiguration mit anderem Datenbank-Passwort: Rolle angleichen
    if [ -n "$OLD_DB_PASSWORD" ] && [ "$OLD_DB_PASSWORD" != "$(get_env_var POSTGRES_PASSWORD)" ]; then
        run_step "Datenbank-Passwort angleichen" sync_db_password ||
            error "Das Datenbank-Passwort ließ sich nicht angleichen (Details: $BACKUP_LOG). $ABORT_HINT"
    fi
    OLD_DB_PASSWORD=""

    # 8. Datenbanken
    if ! run_step "Mandari-DB wiederherstellen" restore_database postgres.sql "$DB_NAME" "$DB_USER"; then
        error "Die Mandari-DB ließ sich nicht einspielen; die bisherige Datenbank ist unverändert (Details: $BACKUP_LOG). $ABORT_HINT"
    fi
    # Ereignistechnik: Die Folgenummer über alles heben, was vor dem Ausfall vergeben wurde, bevor der
    # Worker startet – sonst vergäbe er Nummern, die Suchindex und Feed-Abnehmer schon kennen
    # (docs/BACKUP.md, „Journal und Aufträge“). Ohne Ereignistechnik in der Sicherung tut der Befehl nichts.
    SEQUENCE_RAISED=true
    if ! run_step "Folgenummer anheben" volume_container folgenummer "$APP_SERVICE" \
        python manage.py events_after_restore --apply < /dev/null; then
        SEQUENCE_RAISED=false
        RESTORE_PROBLEMS+=("Folgenummer")
    fi
    if archive_has postgres_website.sql; then
        if ! run_step "Website-DB wiederherstellen" restore_database postgres_website.sql "$WEBSITE_DB" "$DB_USER"; then
            warn "Website-DB ließ sich nicht einspielen; die bisherige ist unverändert."
            RESTORE_PROBLEMS+=("Website-DB")
        fi
    fi

    # 9. Dateien
    if archive_has media.tar; then
        if ! run_step "Uploads wiederherstellen" restore_volume media.tar "$MEDIA_PATH"; then
            RESTORE_PROBLEMS+=("Uploads")
        fi
    fi
    if archive_has files.tar; then
        if ! run_step "Dokument-Cache wiederherstellen" restore_volume files.tar "$FILES_PATH"; then
            RESTORE_PROBLEMS+=("Dokument-Cache")
        fi
    fi

    # 10. Alle Dienste starten, Schema auf den Stand des Images bringen, Suchindex neu aufbauen
    #     (Elasticsearch wird nicht gesichert)
    if ! run_step "Alle Services starten" docker compose up -d; then
        RESTORE_PROBLEMS+=("Dienststart")
    fi
    if [ "$SEQUENCE_RAISED" != true ]; then
        # Ohne angehobene Folgenummer darf kein Sequenzierer laufen
        docker compose stop worker worker-heavy >> "$BACKUP_LOG" 2>&1 || true
        warn "Worker angehalten. Folgenummer von Hand anheben, dann den Worker starten:"
        warn "  docker exec $APP_CONTAINER python manage.py events_after_restore --apply"
        warn "  docker compose up -d worker worker-heavy"
    fi
    printf "  %-30s " "Mandari"
    if wait_for_healthy "$APP_CONTAINER" 90; then
        printf "\b${GREEN}✓${NC}\n"
    else
        printf "\b${YELLOW}⏳${NC}\n"
    fi

    if ! run_step "Datenbank-Migrationen" docker exec "$APP_CONTAINER" python manage.py migrate --noinput; then
        warn "Migrationen von Hand nachholen: docker exec $APP_CONTAINER python manage.py migrate"
        RESTORE_PROBLEMS+=("Migrationen")
    fi
    # Website nur, wenn ihr Container läuft (Installationen ohne Website haben trotzdem deren Datenbank)
    if archive_has postgres_website.sql &&
        [ "$(docker inspect -f '{{.State.Running}}' "$WEBSITE_CONTAINER" 2>/dev/null)" = true ]; then
        if ! run_step "Website-Migrationen" docker exec "$WEBSITE_CONTAINER" python manage.py migrate --noinput; then
            warn "Website-Migrationen von Hand nachholen: docker exec $WEBSITE_CONTAINER python manage.py migrate"
        fi
    fi

    if ! { run_step "Suchindex einrichten" docker exec "$APP_CONTAINER" python manage.py setup_elasticsearch &&
        run_step "Suchindex neu aufbauen" docker exec "$APP_CONTAINER" python manage.py reindex_elasticsearch --clear; }; then
        warn "Suchindex von Hand neu aufbauen:"
        warn "  docker exec $APP_CONTAINER python manage.py setup_elasticsearch"
        warn "  docker exec $APP_CONTAINER python manage.py reindex_elasticsearch --clear"
    fi

    verify_installation

    echo ""
    if [ ${#RESTORE_PROBLEMS[@]} -gt 0 ]; then
        echo -e "${YELLOW}============================================${NC}"
        echo -e "${YELLOW}  Wiederherstellung unvollständig${NC}"
        echo -e "${YELLOW}============================================${NC}"
    else
        echo -e "${GREEN}============================================${NC}"
        echo -e "${GREEN}  Wiederherstellung abgeschlossen!${NC}"
        echo -e "${GREEN}============================================${NC}"
    fi
    echo ""
    echo "  Quelle:  $RESTORE_FILE"
    if [ -n "$PREVIOUS_ENV" ]; then
        echo "  Bisherige Konfiguration: $PREVIOUS_ENV (enthält Schlüssel – verwahren oder löschen)"
    fi
    echo "  Logs:    cat $BACKUP_LOG"
    echo ""
    if [ ${#RESTORE_PROBLEMS[@]} -gt 0 ]; then
        error "Nicht wiederhergestellt: ${RESTORE_PROBLEMS[*]} (Details: $BACKUP_LOG)"
    fi
    exit 0
fi

# =============================================================================
# Backup Mode
# =============================================================================
if [ "$QUIET" = false ]; then
    log "Mandari Backup"
    echo "============================================"
fi

# Check prerequisites
if [ ! -f ".env" ]; then
    error "Keine .env Datei gefunden. Ist Mandari installiert?"
fi

# Load environment variables safely (no source = no code injection)
POSTGRES_USER=$(get_env_var POSTGRES_USER mandari)
POSTGRES_DB=$(get_env_var POSTGRES_DB mandari)
WEBSITE_DB=$(get_env_var WEBSITE_DB mandari_website)
# (Elasticsearch benötigt keinen API-Key)
DOMAIN=$(get_env_var DOMAIN unknown)
IMAGE_TAG=$(get_env_var IMAGE_TAG latest)

INCLUDE_MEDIA=true
INCLUDE_FILES=true
if [ "$NO_MEDIA_FLAG" = true ] || is_true "$(setting BACKUP_NO_MEDIA)"; then
    INCLUDE_MEDIA=false
fi
if [ "$NO_FILES_FLAG" = true ] || is_true "$(setting BACKUP_NO_FILES)"; then
    INCLUDE_FILES=false
fi

# Create backup directory
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"
CLEANUP_PATHS+=("$BACKUP_PATH")
BACKUP_PROBLEMS=()

info "Erstelle Backup: ${CYAN}$BACKUP_NAME${NC}"

# =============================================================================
# Backup Configuration (nur verschlüsselt)
# =============================================================================
CONFIG_STATE="nicht gesichert"
if [ -n "$(get_env_var BACKUP_PASSPHRASE)" ]; then
    warn "BACKUP_PASSPHRASE in .env wird ignoriert – die Passphrase gehört nicht in die Datei, die sie schützt."
fi
if ! load_passphrase; then
    BACKUP_PROBLEMS+=("Konfiguration (Passphrase-Datei)")
elif [ -z "$PASSPHRASE" ]; then
    warn "Konfiguration (.env) NICHT gesichert: keine Passphrase (BACKUP_PASSPHRASE oder BACKUP_PASSPHRASE_FILE, siehe DEPLOYMENT.md)."
    warn "  Ohne .env (ENCRYPTION_MASTER_KEY) sind verschlüsselte Inhalte nach einer Wiederherstellung nicht lesbar – .env getrennt verwahren."
elif ! command -v openssl &>/dev/null; then
    warn "openssl nicht gefunden – Konfiguration nicht gesichert."
    BACKUP_PROBLEMS+=("Konfiguration (openssl fehlt)")
else
    if [ ${#PASSPHRASE} -lt $MIN_PASSPHRASE_LENGTH ]; then
        warn "Die Passphrase ist kürzer als $MIN_PASSPHRASE_LENGTH Zeichen – bitte eine längere verwenden (z. B. openssl rand -base64 32)."
    fi
    # Nach dem Verschlüsseln einmal zurück entschlüsseln und vergleichen (nur im Speicher)
    if run_step "Konfiguration verschlüsseln" encrypt_config .env "$BACKUP_PATH/config.env.enc" &&
        decrypt_config "$BACKUP_PATH/config.env.enc" 2>> "$BACKUP_LOG" | cmp -s - .env; then
        CONFIG_STATE="verschlüsselt"
    else
        rm -f "$BACKUP_PATH/config.env.enc"
        warn "Konfiguration ließ sich nicht verschlüsseln (Details: $BACKUP_LOG) – nicht gesichert."
        BACKUP_PROBLEMS+=("Konfiguration")
    fi
fi

# =============================================================================
# Backup PostgreSQL
# =============================================================================
if run_step_to_file "Mandari-DB sichern" "$BACKUP_PATH/postgres.sql" \
    docker exec "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"; then
    DB_DUMP_SIZE=$(human_size "$BACKUP_PATH/postgres.sql")
    info "  Mandari-DB: $DB_DUMP_SIZE"
else
    error "Datenbank-Backup fehlgeschlagen. Läuft PostgreSQL?"
fi

# Website-Datenbank (Wagtail) — falls vorhanden
WEBSITE_STATE="nicht vorhanden"
if docker exec "$DB_CONTAINER" psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database" 2>/dev/null |
    grep -qxF "$WEBSITE_DB"; then
    if run_step_to_file "Website-DB sichern" "$BACKUP_PATH/postgres_website.sql" \
        docker exec "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$WEBSITE_DB"; then
        WEBSITE_STATE="gesichert"
        info "  Website-DB: $(human_size "$BACKUP_PATH/postgres_website.sql")"
    else
        rm -f "$BACKUP_PATH/postgres_website.sql"
        warn "  Website-DB-Backup fehlgeschlagen"
        BACKUP_PROBLEMS+=("Website-DB")
    fi
fi

# =============================================================================
# Backup Files (Uploads, Dokument-Cache)
# =============================================================================
MEDIA_STATE="ausgelassen"
if [ "$INCLUDE_MEDIA" = true ]; then
    if run_step_to_file "Uploads sichern" "$BACKUP_PATH/media.tar" volume_tar_create "$MEDIA_PATH"; then
        MEDIA_STATE="gesichert"
        info "  Uploads: $(human_size "$BACKUP_PATH/media.tar")"
    else
        rm -f "$BACKUP_PATH/media.tar"
        warn "  Uploads ließen sich nicht sichern (Details: $BACKUP_LOG)"
        BACKUP_PROBLEMS+=("Uploads")
    fi
else
    info "  Uploads: ausgelassen (--no-media)"
fi

FILES_STATE="ausgelassen"
if [ "$INCLUDE_FILES" = true ]; then
    if run_step_to_file "Dokument-Cache sichern" "$BACKUP_PATH/files.tar" volume_tar_create "$FILES_PATH"; then
        FILES_STATE="gesichert"
        info "  Dokument-Cache: $(human_size "$BACKUP_PATH/files.tar")"
        files_kb=$(du -k "$BACKUP_PATH/files.tar" 2>/dev/null | cut -f1)
        if [ "${files_kb:-0}" -gt "$FILES_SIZE_HINT_KB" ]; then
            info "  Der Dokument-Cache ist groß. Er lässt sich aus den Ratsinformationssystemen neu"
            info "  laden; mit --no-files (oder BACKUP_NO_FILES=true in .env) bleibt er außen vor."
        fi
    else
        rm -f "$BACKUP_PATH/files.tar"
        warn "  Dokument-Cache ließ sich nicht sichern (Details: $BACKUP_LOG)"
        BACKUP_PROBLEMS+=("Dokument-Cache")
    fi
else
    info "  Dokument-Cache: ausgelassen (--no-files)"
fi

# =============================================================================
# Elasticsearch (Suchindex wird beim Restore neu aufgebaut)
# =============================================================================
info "  Elasticsearch-Index: wird beim Restore neu aufgebaut"

# =============================================================================
# Backup Docker Volumes Info
# =============================================================================
run_step_to_file "Volume-Informationen" "$BACKUP_PATH/volumes.txt" \
    docker volume ls --filter name="$COMPOSE_PROJECT_NAME" || true

# =============================================================================
# Backup Metadata
# =============================================================================
cat > "$BACKUP_PATH/metadata.json" << EOF
{
    "format": 2,
    "timestamp": "$(date -Iseconds)",
    "hostname": "$(json_escape "$(hostname)")",
    "domain": "$(json_escape "${DOMAIN:-unknown}")",
    "mandari_version": "$(json_escape "${IMAGE_TAG:-latest}")",
    "docker_version": "$(json_escape "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'unknown')")",
    "website_database": "$WEBSITE_STATE",
    "media": "$MEDIA_STATE",
    "files": "$FILES_STATE",
    "config": "$CONFIG_STATE",
    "config_cipher": "$CONFIG_CIPHER_LABEL"
}
EOF

# =============================================================================
# Create Archive
# =============================================================================
# Kleine Bestandteile zuerst: Wiederherstellung und Prüfung finden sie, ohne die großen
# Datei-Archive lesen zu müssen.
archive_members=()
for part in metadata.json config.env.enc volumes.txt postgres.sql postgres_website.sql media.tar files.tar; do
    if [ -f "$BACKUP_PATH/$part" ]; then
        archive_members+=("$BACKUP_NAME/$part")
    fi
done
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
if ! run_step "Archiv erstellen" tar -czf "$ARCHIVE_FILE" -C "$BACKUP_DIR" "${archive_members[@]}"; then
    rm -f "$ARCHIVE_FILE"
    error "Archiv ließ sich nicht erstellen (Platz im Zielverzeichnis? Details: $BACKUP_LOG)"
fi

# Cleanup temp directory
rm -rf "$BACKUP_PATH"

# Get archive size
ARCHIVE_SIZE=$(human_size "$ARCHIVE_FILE")

# =============================================================================
# Verify (optional)
# =============================================================================
VERIFY_FAILED=false
verify_item() {
    local label="$1"
    local state="$2"
    local detail="${3:-}"
    printf "  %-30s " "$label"
    case "$state" in
        ok) echo -e "${GREEN}✓${NC}${detail:+ ($detail)}" ;;
        skip) echo -e "${YELLOW}–${NC}${detail:+ $detail}" ;;
        *) echo -e "${RED}✗${NC}${detail:+ $detail}"; VERIFY_FAILED=true ;;
    esac
}

if [ "$VERIFY" = true ]; then
    log "Backup-Integrität prüfen..."

    VERIFY_DIR=$(mktemp -d)
    CLEANUP_PATHS+=("$VERIFY_DIR")
    ARCHIVE="$ARCHIVE_FILE"
    ARCHIVE_ROOT="$BACKUP_NAME"
    ARCHIVE_LISTING="$VERIFY_DIR/inhalt.txt"

    # Liest das ganze Archiv und prüft dabei die Prüfsumme
    if archive_list "$ARCHIVE" "$ARCHIVE_LISTING" 2>> "$BACKUP_LOG"; then
        verify_item "Archiv-Integrität" ok
    else
        verify_item "Archiv-Integrität" fail
        error "  Archiv ist beschädigt!"
    fi

    { archive_cat postgres.sql 2>/dev/null || true; } | head -c 4096 > "$VERIFY_DIR/postgres.head" || true
    if grep -q "PostgreSQL database dump" "$VERIFY_DIR/postgres.head"; then
        verify_item "Datenbank-Dump" ok "$DB_DUMP_SIZE"
    else
        verify_item "Datenbank-Dump" fail "Header nicht gefunden"
    fi

    for entry in "postgres_website.sql:Website-DB:$WEBSITE_STATE" "media.tar:Uploads:$MEDIA_STATE" "files.tar:Dokument-Cache:$FILES_STATE"; do
        part="${entry%%:*}"
        rest="${entry#*:}"
        label="${rest%%:*}"
        state="${rest#*:}"
        if archive_has "$part"; then
            verify_item "$label" ok
        elif [ "$state" = "gesichert" ]; then
            verify_item "$label" fail "fehlt im Archiv"
        else
            verify_item "$label" skip "$state"
        fi
    done

    if archive_has config.env.enc; then
        if [ -n "$PASSPHRASE" ] && archive_cat config.env.enc > "$VERIFY_DIR/config.env.enc" 2>> "$BACKUP_LOG" &&
            decrypt_config "$VERIFY_DIR/config.env.enc" 2>> "$BACKUP_LOG" | grep '^SECRET_KEY=' > /dev/null; then
            verify_item "Konfiguration (verschlüsselt)" ok
        else
            verify_item "Konfiguration (verschlüsselt)" fail "nicht entschlüsselbar"
        fi
    else
        verify_item "Konfiguration" skip "nicht gesichert"
    fi

    if archive_has metadata.json; then
        verify_item "Metadaten" ok
    else
        verify_item "Metadaten" fail
    fi

    echo ""
    if [ "$VERIFY_FAILED" = true ]; then
        BACKUP_PROBLEMS+=("Prüfung")
    fi
fi

# =============================================================================
# Optional: S3 Upload
# =============================================================================
if [ -n "${S3_BACKUP_BUCKET:-}" ]; then
    if command -v aws &>/dev/null; then
        run_step "S3-Upload" aws s3 cp "$ARCHIVE_FILE" "s3://$S3_BACKUP_BUCKET/mandari/" || \
            warn "  S3-Upload fehlgeschlagen"
    else
        warn "  AWS CLI nicht installiert. S3-Upload übersprungen."
    fi
fi

# =============================================================================
# Summary
# =============================================================================
if [ "$QUIET" = false ]; then
    echo ""
    if [ ${#BACKUP_PROBLEMS[@]} -gt 0 ]; then
        echo -e "${YELLOW}============================================${NC}"
        echo -e "${YELLOW}  Backup unvollständig${NC}"
        echo -e "${YELLOW}============================================${NC}"
    else
        echo -e "${GREEN}============================================${NC}"
        echo -e "${GREEN}  Backup abgeschlossen!${NC}"
        echo -e "${GREEN}============================================${NC}"
    fi
    echo ""
    echo -e "  Datei:   $ARCHIVE_FILE"
    echo -e "  Größe:   $ARCHIVE_SIZE"
    echo ""
    echo "  Inhalt:"
    echo "    - Mandari-DB"
    echo "    - Website-DB: $WEBSITE_STATE"
    echo "    - Uploads: $MEDIA_STATE"
    echo "    - Dokument-Cache: $FILES_STATE"
    echo "    - Konfiguration (.env): $CONFIG_STATE"
    echo "    - Volume-Informationen, Backup-Metadaten"
    echo ""
    echo "  Wiederherstellen:"
    echo "    ./backup.sh --restore $ARCHIVE_FILE"
    echo ""
    echo "  Logs:    cat $BACKUP_LOG"
    echo ""
fi

# =============================================================================
# Cleanup Old Backups (Retention: letzte KEEP_ARCHIVES)
# =============================================================================
# Nach einer unvollständigen Sicherung wird nichts gelöscht, damit wiederholte Fehler
# nicht nach und nach alle vollständigen Stände verdrängen.
if [ ${#BACKUP_PROBLEMS[@]} -gt 0 ]; then
    error "Backup unvollständig: ${BACKUP_PROBLEMS[*]} (Archiv: $ARCHIVE_FILE, Details: $BACKUP_LOG). Alte Backups bleiben erhalten."
fi

BACKUP_COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -name 'mandari_backup_*.tar.gz' -type f | wc -l)
if [ "$BACKUP_COUNT" -gt "$KEEP_ARCHIVES" ]; then
    remove_old_backups() {
        ls -1t "$BACKUP_DIR"/mandari_backup_*.tar.gz | tail -n +$((KEEP_ARCHIVES + 1)) | xargs -r rm -f
    }
    run_step "Alte Backups aufräumen" remove_old_backups || warn "Alte Backups ließen sich nicht aufräumen"
fi
