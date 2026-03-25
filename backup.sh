#!/bin/bash
# =============================================================================
# Mandari - Backup Script
# =============================================================================
# Creates a complete backup of all Mandari data
#
# Usage:
#   ./backup.sh                    # Create backup in ./backups/
#   ./backup.sh /path/to/backups   # Create backup in custom directory
#   ./backup.sh --restore FILE     # Restore from backup file
#   ./backup.sh --quiet            # Only output errors (for cron)
#   ./backup.sh --verify           # Verify backup integrity after creation
# =============================================================================

set -euo pipefail

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

# Sicheres Lesen einzelner Werte aus .env (kein source = keine Code-Injection)
get_env_var() {
    local key="$1"
    local default="${2:-}"
    local val
    val=$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d'=' -f2-)
    echo "${val:-$default}"
}

# Run a command with spinner, output goes to log file
# Usage: run_step "Description" command arg1 arg2 ...
run_step() {
    if [ "$QUIET" = true ]; then
        # Im Quiet-Modus: direkt ausführen, nur Fehler ausgeben
        "$@" >> "$BACKUP_LOG" 2>&1
        return $?
    fi

    local description="$1"
    shift

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
    wait "$pid"
    local exit_code=$?

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
    if [ "$QUIET" = true ]; then
        local out="$1"
        shift
        "$@" > "$out" 2>> "$BACKUP_LOG"
        return $?
    fi

    local description="$1"
    local output_file="$2"
    shift 2

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
    wait "$pid"
    local exit_code=$?

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

    for container in mandari-postgres mandari-redis mandari-elasticsearch mandari mandari-website mandari-caddy mandari-ingestor; do
        local status
        local health
        status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
        health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "no-healthcheck")

        local label
        case "$container" in
            mandari-postgres)    label="PostgreSQL" ;;
            mandari-redis)       label="Redis" ;;
            mandari-elasticsearch) label="Elasticsearch" ;;
            mandari)             label="Mandari" ;;
            mandari-website)     label="Website" ;;
            mandari-caddy)       label="Caddy" ;;
            mandari-ingestor)    label="Ingestor" ;;
        esac

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

# =============================================================================
# Parse Arguments
# =============================================================================
RESTORE_MODE=false
RESTORE_FILE=""

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
        --help|-h)
            echo "Usage: $0 [BACKUP_DIR] [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  BACKUP_DIR       Backup-Verzeichnis (default: ./backups/)"
            echo "  --restore FILE   Aus Backup-Datei wiederherstellen"
            echo "  --quiet          Nur Fehler ausgeben (für Cron-Jobs)"
            echo "  --verify         Backup-Integrität nach Erstellung prüfen"
            echo "  -h, --help       Diese Hilfe anzeigen"
            exit 0
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

    warn "ACHTUNG: Alle aktuellen Daten werden überschrieben!"
    read -p "Sicher wiederherstellen aus $RESTORE_FILE? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        log "Wiederherstellung abgebrochen"
        exit 0
    fi

    log "Wiederherstellung aus Backup..."

    # Create temp directory
    RESTORE_DIR=$(mktemp -d)
    trap "rm -rf $RESTORE_DIR" EXIT

    # Extract backup
    run_step "Backup entpacken" tar -xzf "$RESTORE_FILE" -C "$RESTORE_DIR"

    BACKUP_CONTENT="$RESTORE_DIR/$(ls "$RESTORE_DIR")"

    # Stop services
    run_step "Services stoppen" docker compose down

    # Restore .env
    if [ -f "$BACKUP_CONTENT/.env" ]; then
        run_step "Konfiguration wiederherstellen" cp "$BACKUP_CONTENT/.env" .env
        chmod 600 .env
    fi

    # Start only postgres — wait for healthy statt sleep
    log "Datenbank starten..."
    docker compose up -d postgres >> "$BACKUP_LOG" 2>&1
    printf "  %-30s " "PostgreSQL"
    if wait_for_healthy mandari-postgres 30; then
        printf "\b${GREEN}✓ healthy${NC}\n"
    else
        printf "\b${YELLOW}⏳${NC}\n"
        sleep 5
    fi

    # Restore database
    if [ -f "$BACKUP_CONTENT/postgres.sql" ]; then
        local_restore_user=$(get_env_var POSTGRES_USER mandari)
        local_restore_db=$(get_env_var POSTGRES_DB mandari)
        run_step "Mandari-DB wiederherstellen" docker exec -i mandari-postgres psql -U "$local_restore_user" "$local_restore_db" < "$BACKUP_CONTENT/postgres.sql"
    fi

    # Restore website DB if present
    if [ -f "$BACKUP_CONTENT/postgres_website.sql" ]; then
        local_website_db=$(get_env_var WEBSITE_DB mandari_website)
        local_restore_user=$(get_env_var POSTGRES_USER mandari)
        run_step "Website-DB wiederherstellen" docker exec -i mandari-postgres psql -U "$local_restore_user" "$local_website_db" < "$BACKUP_CONTENT/postgres_website.sql"
    fi

    # Elasticsearch: Index wird beim Start automatisch neu aufgebaut
    # (setup_elasticsearch + reindex_elasticsearch)
    log "Elasticsearch-Index wird nach dem Start automatisch neu aufgebaut."

    # Start all services
    run_step "Alle Services starten" docker compose up -d

    # Wait for main services
    sleep 3
    verify_installation

    run_step "Suchindex neu aufbauen" docker exec mandari python manage.py rebuild_search_index || \
        warn "Suchindex-Rebuild ggf. manuell nötig"

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Wiederherstellung abgeschlossen!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo "  Quelle:  $RESTORE_FILE"
    echo "  Logs:    cat $BACKUP_LOG"
    echo ""
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

# Create backup directory
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

info "Erstelle Backup: ${CYAN}$BACKUP_NAME${NC}"

# =============================================================================
# Backup Configuration
# =============================================================================
run_step "Konfiguration sichern" cp .env "$BACKUP_PATH/.env"

# =============================================================================
# Backup PostgreSQL
# =============================================================================
if run_step_to_file "Mandari-DB sichern" "$BACKUP_PATH/postgres.sql" \
    docker exec mandari-postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"; then
    if [ "$QUIET" = false ]; then
        DB_SIZE=$(du -h "$BACKUP_PATH/postgres.sql" | cut -f1)
        info "  Mandari-DB: $DB_SIZE"
    fi
else
    error "Datenbank-Backup fehlgeschlagen. Läuft PostgreSQL?"
fi

# Website-Datenbank (Wagtail) — falls vorhanden
if docker exec mandari-postgres psql -U "$POSTGRES_USER" -lqt 2>/dev/null | grep -q "$WEBSITE_DB"; then
    if run_step_to_file "Website-DB sichern" "$BACKUP_PATH/postgres_website.sql" \
        docker exec mandari-postgres pg_dump -U "$POSTGRES_USER" "$WEBSITE_DB"; then
        if [ "$QUIET" = false ]; then
            WDB_SIZE=$(du -h "$BACKUP_PATH/postgres_website.sql" | cut -f1)
            info "  Website-DB: $WDB_SIZE"
        fi
    else
        warn "  Website-DB-Backup fehlgeschlagen"
    fi
fi

# =============================================================================
# Elasticsearch (Suchindex wird beim Restore automatisch neu aufgebaut)
# =============================================================================
if [ "$QUIET" = false ]; then
    info "  Elasticsearch-Index: wird beim Restore automatisch neu aufgebaut"
fi

# =============================================================================
# Backup Docker Volumes Info
# =============================================================================
run_step "Volume-Informationen" docker volume ls --filter name=mandari > "$BACKUP_PATH/volumes.txt" 2>/dev/null || true

# =============================================================================
# Backup Metadata
# =============================================================================
cat > "$BACKUP_PATH/metadata.json" << EOF
{
    "timestamp": "$(date -Iseconds)",
    "hostname": "$(hostname)",
    "domain": "${DOMAIN:-unknown}",
    "mandari_version": "${IMAGE_TAG:-latest}",
    "docker_version": "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'unknown')"
}
EOF

# =============================================================================
# Create Archive
# =============================================================================
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
run_step "Archiv erstellen" tar -czf "$ARCHIVE_FILE" -C "$BACKUP_DIR" "$BACKUP_NAME"

# Cleanup temp directory
rm -rf "$BACKUP_PATH"

# Get archive size
ARCHIVE_SIZE=$(du -h "$ARCHIVE_FILE" | cut -f1)

# =============================================================================
# Verify (optional)
# =============================================================================
if [ "$VERIFY" = true ]; then
    log "Backup-Integrität prüfen..."

    VERIFY_DIR=$(mktemp -d)
    trap "rm -rf $VERIFY_DIR" EXIT

    # tar-Archiv testen
    printf "  %-30s " "Archiv-Integrität"
    if tar -tzf "$ARCHIVE_FILE" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC}"
    else
        echo -e "${RED}✗${NC}"
        error "  Archiv ist beschädigt!"
    fi

    # Inhalt extrahieren und pg_dump prüfen
    tar -xzf "$ARCHIVE_FILE" -C "$VERIFY_DIR"
    VERIFY_CONTENT="$VERIFY_DIR/$(ls "$VERIFY_DIR")"

    if [ -f "$VERIFY_CONTENT/postgres.sql" ]; then
        printf "  %-30s " "Datenbank-Dump"
        if head -5 "$VERIFY_CONTENT/postgres.sql" | grep -q "PostgreSQL database dump"; then
            sql_size=$(du -h "$VERIFY_CONTENT/postgres.sql" | cut -f1)
            echo -e "${GREEN}✓${NC} ($sql_size)"
        else
            echo -e "${YELLOW}⚠ Header nicht gefunden${NC}"
        fi
    fi

    printf "  %-30s " "Konfiguration"
    if [ -f "$VERIFY_CONTENT/.env" ]; then
        echo -e "${GREEN}✓${NC}"
    else
        echo -e "${RED}✗${NC}"
    fi

    printf "  %-30s " "Metadaten"
    if [ -f "$VERIFY_CONTENT/metadata.json" ]; then
        echo -e "${GREEN}✓${NC}"
    else
        echo -e "${RED}✗${NC}"
    fi

    rm -rf "$VERIFY_DIR"
    echo ""
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
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Backup abgeschlossen!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  Datei:   $ARCHIVE_FILE"
    echo -e "  Größe:   $ARCHIVE_SIZE"
    echo ""
    echo "  Inhalt:"
    echo "    - Konfiguration (.env)"
    echo "    - PostgreSQL-Datenbank"
    echo "    - Volume-Informationen"
    echo "    - Backup-Metadaten"
    echo ""
    echo "  Wiederherstellen:"
    echo "    ./backup.sh --restore $ARCHIVE_FILE"
    echo ""
    echo "  Logs:    cat $BACKUP_LOG"
    echo ""
fi

# =============================================================================
# Cleanup Old Backups (Retention: letzte 7)
# =============================================================================
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/*.tar.gz 2>/dev/null | wc -l || echo 0)
if [ "$BACKUP_COUNT" -gt 7 ]; then
    if [ "$QUIET" = false ]; then
        run_step "Alte Backups aufräumen" bash -c "ls -1t '$BACKUP_DIR'/*.tar.gz | tail -n +8 | xargs rm -f"
    else
        ls -1t "$BACKUP_DIR"/*.tar.gz | tail -n +8 | xargs rm -f
    fi
fi
