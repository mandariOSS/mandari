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
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BACKUP_DIR="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="mandari_backup_${TIMESTAMP}"

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
    echo -e "${GREEN}[BACKUP]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# =============================================================================
# Restore Mode
# =============================================================================
if [ "${1:-}" = "--restore" ]; then
    RESTORE_FILE="${2:-}"
    if [ -z "$RESTORE_FILE" ]; then
        error "Usage: $0 --restore BACKUP_FILE"
    fi
    if [ ! -f "$RESTORE_FILE" ]; then
        error "Backup file not found: $RESTORE_FILE"
    fi

    warn "This will OVERWRITE all current data!"
    read -p "Are you sure you want to restore from $RESTORE_FILE? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        log "Restore cancelled"
        exit 0
    fi

    log "Restoring from backup..."

    # Create temp directory
    RESTORE_DIR=$(mktemp -d)
    trap "rm -rf $RESTORE_DIR" EXIT

    # Extract backup
    log "Extracting backup..."
    tar -xzf "$RESTORE_FILE" -C "$RESTORE_DIR"

    BACKUP_CONTENT="$RESTORE_DIR/$(ls "$RESTORE_DIR")"

    # Stop services
    log "Stopping services..."
    docker compose down

    # Restore .env
    if [ -f "$BACKUP_CONTENT/.env" ]; then
        log "Restoring configuration..."
        cp "$BACKUP_CONTENT/.env" .env
        chmod 600 .env
    fi

    # Start only postgres
    log "Starting database..."
    docker compose up -d postgres
    sleep 10

    # Restore database
    if [ -f "$BACKUP_CONTENT/postgres.sql" ]; then
        log "Restoring database..."
        # Load environment
        source .env
        docker exec -i mandari-postgres psql -U "${POSTGRES_USER:-mandari}" "${POSTGRES_DB:-mandari}" < "$BACKUP_CONTENT/postgres.sql"
    fi

    # Restore Meilisearch data
    if [ -f "$BACKUP_CONTENT/meilisearch.tar" ]; then
        log "Restoring search index..."
        docker compose up -d meilisearch
        sleep 5
        # Note: Meilisearch data restore would need volume access
        warn "Meilisearch data backup found. Manual restore may be needed."
    fi

    # Start all services
    log "Starting all services..."
    docker compose up -d

    log "Restore completed!"
    log "Rebuilding search index..."
    docker exec mandari-api python manage.py rebuild_search_index || warn "Search index rebuild may be needed"

    exit 0
fi

# =============================================================================
# Backup Mode
# =============================================================================
log "Mandari Backup"
echo "============================================"

# Check prerequisites
if [ ! -f ".env" ]; then
    error "No .env file found. Is Mandari installed?"
fi

# Load environment
source .env

# Create backup directory
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

log "Creating backup: $BACKUP_NAME"

# =============================================================================
# Backup Configuration
# =============================================================================
log "Backing up configuration..."
cp .env "$BACKUP_PATH/.env"

# =============================================================================
# Backup PostgreSQL
# =============================================================================
log "Backing up database..."
if docker exec mandari-postgres pg_dump -U "${POSTGRES_USER:-mandari}" "${POSTGRES_DB:-mandari}" > "$BACKUP_PATH/postgres.sql" 2>/dev/null; then
    DB_SIZE=$(du -h "$BACKUP_PATH/postgres.sql" | cut -f1)
    log "  Database: $DB_SIZE"
else
    error "Database backup failed. Is PostgreSQL running?"
fi

# =============================================================================
# Backup Meilisearch (optional - can be rebuilt)
# =============================================================================
log "Backing up search index..."
if docker exec mandari-meilisearch curl -sf http://localhost:7700/health &>/dev/null; then
    # Create a snapshot
    SNAPSHOT_RESULT=$(docker exec mandari-meilisearch curl -sf -X POST http://localhost:7700/snapshots \
        -H "Authorization: Bearer ${MEILISEARCH_KEY}" 2>/dev/null || echo "")
    if [ -n "$SNAPSHOT_RESULT" ]; then
        log "  Meilisearch snapshot created"
    else
        warn "  Meilisearch snapshot creation failed (index can be rebuilt)"
    fi
else
    warn "  Meilisearch not running. Skipping search backup."
fi

# =============================================================================
# Backup Docker Volumes Info
# =============================================================================
log "Recording volume information..."
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
log "Creating archive..."
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
tar -czf "$ARCHIVE_FILE" -C "$BACKUP_DIR" "$BACKUP_NAME"

# Cleanup temp directory
rm -rf "$BACKUP_PATH"

# Get archive size
ARCHIVE_SIZE=$(du -h "$ARCHIVE_FILE" | cut -f1)

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Backup Complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "  File: $ARCHIVE_FILE"
echo "  Size: $ARCHIVE_SIZE"
echo ""
echo "  Contents:"
echo "    - Configuration (.env)"
echo "    - PostgreSQL database"
echo "    - Volume information"
echo "    - Backup metadata"
echo ""
echo "  To restore:"
echo "    ./backup.sh --restore $ARCHIVE_FILE"
echo ""

# =============================================================================
# Cleanup Old Backups
# =============================================================================
# Keep only last 7 backups
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/*.tar.gz 2>/dev/null | wc -l || echo 0)
if [ "$BACKUP_COUNT" -gt 7 ]; then
    log "Cleaning up old backups (keeping last 7)..."
    ls -1t "$BACKUP_DIR"/*.tar.gz | tail -n +8 | xargs rm -f
fi

log "Done!"
