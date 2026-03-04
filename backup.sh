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
NC='\033[0m'

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
    log "Backup entpacken..."
    tar -xzf "$RESTORE_FILE" -C "$RESTORE_DIR"

    BACKUP_CONTENT="$RESTORE_DIR/$(ls "$RESTORE_DIR")"

    # Stop services
    log "Services stoppen..."
    docker compose down

    # Restore .env
    if [ -f "$BACKUP_CONTENT/.env" ]; then
        log "Konfiguration wiederherstellen..."
        cp "$BACKUP_CONTENT/.env" .env
        chmod 600 .env
    fi

    # Start only postgres
    log "Datenbank starten..."
    docker compose up -d postgres
    sleep 10

    # Restore database
    if [ -f "$BACKUP_CONTENT/postgres.sql" ]; then
        log "Datenbank wiederherstellen..."
        source .env
        docker exec -i mandari-postgres psql -U "${POSTGRES_USER:-mandari}" "${POSTGRES_DB:-mandari}" < "$BACKUP_CONTENT/postgres.sql"
    fi

    # Restore Meilisearch data
    if [ -f "$BACKUP_CONTENT/meilisearch.tar" ]; then
        log "Suchindex wiederherstellen..."
        docker compose up -d meilisearch
        sleep 5
        warn "Meilisearch-Daten gefunden. Manuelle Wiederherstellung ggf. nötig."
    fi

    # Start all services
    log "Alle Services starten..."
    docker compose up -d

    log "Wiederherstellung abgeschlossen!"
    log "Suchindex neu aufbauen..."
    docker exec mandari python manage.py rebuild_search_index || warn "Suchindex-Rebuild ggf. manuell nötig"

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

# Load environment
source .env

# Create backup directory
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

log "Erstelle Backup: $BACKUP_NAME"

# =============================================================================
# Backup Configuration
# =============================================================================
log "Konfiguration sichern..."
cp .env "$BACKUP_PATH/.env"

# =============================================================================
# Backup PostgreSQL
# =============================================================================
log "Datenbank sichern..."
if docker exec mandari-postgres pg_dump -U "${POSTGRES_USER:-mandari}" "${POSTGRES_DB:-mandari}" > "$BACKUP_PATH/postgres.sql" 2>/dev/null; then
    DB_SIZE=$(du -h "$BACKUP_PATH/postgres.sql" | cut -f1)
    log "  Datenbank: $DB_SIZE"
else
    error "Datenbank-Backup fehlgeschlagen. Läuft PostgreSQL?"
fi

# =============================================================================
# Backup Meilisearch (optional - kann neu aufgebaut werden)
# =============================================================================
log "Suchindex sichern..."
if docker exec mandari-meilisearch curl -sf http://localhost:7700/health &>/dev/null; then
    SNAPSHOT_RESULT=$(docker exec mandari-meilisearch curl -sf -X POST http://localhost:7700/snapshots \
        -H "Authorization: Bearer ${MEILISEARCH_KEY}" 2>/dev/null || echo "")
    if [ -n "$SNAPSHOT_RESULT" ]; then
        log "  Meilisearch-Snapshot erstellt"
    else
        warn "  Meilisearch-Snapshot fehlgeschlagen (Index kann neu aufgebaut werden)"
    fi
else
    warn "  Meilisearch läuft nicht. Suchindex-Backup übersprungen."
fi

# =============================================================================
# Backup Docker Volumes Info
# =============================================================================
log "Volume-Informationen erfassen..."
docker volume ls --filter name=mandari > "$BACKUP_PATH/volumes.txt" 2>/dev/null || true

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
log "Archiv erstellen..."
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
tar -czf "$ARCHIVE_FILE" -C "$BACKUP_DIR" "$BACKUP_NAME"

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
    if tar -tzf "$ARCHIVE_FILE" > /dev/null 2>&1; then
        log "  Archiv-Integrität: OK"
    else
        error "  Archiv-Integrität: FEHLGESCHLAGEN — Backup ist beschädigt!"
    fi

    # Inhalt extrahieren und pg_dump prüfen
    tar -xzf "$ARCHIVE_FILE" -C "$VERIFY_DIR"
    VERIFY_CONTENT="$VERIFY_DIR/$(ls "$VERIFY_DIR")"

    if [ -f "$VERIFY_CONTENT/postgres.sql" ]; then
        # Prüfe ob pg_dump-Header vorhanden
        if head -5 "$VERIFY_CONTENT/postgres.sql" | grep -q "PostgreSQL database dump"; then
            log "  Datenbank-Dump: OK"
        else
            warn "  Datenbank-Dump: Header nicht gefunden — möglicherweise unvollständig"
        fi

        local sql_size
        sql_size=$(du -h "$VERIFY_CONTENT/postgres.sql" | cut -f1)
        log "  Datenbank-Größe: $sql_size"
    fi

    if [ -f "$VERIFY_CONTENT/.env" ]; then
        log "  Konfiguration: OK"
    fi

    if [ -f "$VERIFY_CONTENT/metadata.json" ]; then
        log "  Metadaten: OK"
    fi

    rm -rf "$VERIFY_DIR"
    log "Verifikation abgeschlossen"
fi

# =============================================================================
# Optional: S3 Upload
# =============================================================================
if [ -n "${S3_BACKUP_BUCKET:-}" ]; then
    log "Backup nach S3 hochladen..."
    if command -v aws &>/dev/null; then
        if aws s3 cp "$ARCHIVE_FILE" "s3://$S3_BACKUP_BUCKET/mandari/" 2>&1; then
            log "  S3-Upload erfolgreich: s3://$S3_BACKUP_BUCKET/mandari/$(basename "$ARCHIVE_FILE")"
        else
            warn "  S3-Upload fehlgeschlagen"
        fi
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
    echo -e "${GREEN}  Backup abgeschlossen${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo "  Datei: $ARCHIVE_FILE"
    echo "  Größe: $ARCHIVE_SIZE"
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
fi

# =============================================================================
# Cleanup Old Backups (Retention: letzte 7)
# =============================================================================
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/*.tar.gz 2>/dev/null | wc -l || echo 0)
if [ "$BACKUP_COUNT" -gt 7 ]; then
    log "Alte Backups aufräumen (behalte letzte 7)..."
    ls -1t "$BACKUP_DIR"/*.tar.gz | tail -n +8 | xargs rm -f
fi

log "Fertig!"
