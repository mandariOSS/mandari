#!/bin/bash
# =============================================================================
# Mandari 2.0 - Backup Script
# =============================================================================
# This script creates backups of the Mandari database and application data.
#
# Usage:
#   ./backup.sh db        - Backup PostgreSQL database
#   ./backup.sh files     - Backup uploaded files
#   ./backup.sh full      - Full backup (db + files)
#   ./backup.sh restore   - Restore from backup
#   ./backup.sh list      - List available backups
# =============================================================================

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/opt/mandari/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Logging
log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"; }
error() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"; exit 1; }

# Load configuration
load_config() {
    if [ -f "$PROJECT_ROOT/.infra_outputs" ]; then
        source "$PROJECT_ROOT/.infra_outputs"
    fi

    if [ -f "$PROJECT_ROOT/docker/.env" ]; then
        set -a
        source "$PROJECT_ROOT/docker/.env"
        set +a
    fi
}

# Ensure backup directory exists
ensure_backup_dir() {
    mkdir -p "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR/db"
    mkdir -p "$BACKUP_DIR/files"
}

# =============================================================================
# Database Backup
# =============================================================================

backup_database() {
    log "Starting PostgreSQL backup..."
    ensure_backup_dir

    BACKUP_FILE="$BACKUP_DIR/db/mandari_db_$TIMESTAMP.sql.gz"

    # Run pg_dump inside container
    docker exec mandari-postgres pg_dump \
        -U "${POSTGRES_USER:-mandari}" \
        -d "${POSTGRES_DB:-mandari}" \
        --format=plain \
        --no-owner \
        --no-acl \
        | gzip > "$BACKUP_FILE"

    # Verify backup
    if [ -f "$BACKUP_FILE" ] && [ -s "$BACKUP_FILE" ]; then
        SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        log "Database backup created: $BACKUP_FILE ($SIZE)"

        # Create checksum
        sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
        log "Checksum created: $BACKUP_FILE.sha256"
    else
        error "Database backup failed!"
    fi

    # Cleanup old backups
    cleanup_old_backups "$BACKUP_DIR/db" "mandari_db_*.sql.gz"
}

# =============================================================================
# Files Backup
# =============================================================================

backup_files() {
    log "Starting files backup..."
    ensure_backup_dir

    DATA_DIR="${DATA_DIR:-/mnt/data}"
    BACKUP_FILE="$BACKUP_DIR/files/mandari_files_$TIMESTAMP.tar.gz"

    # Backup meilisearch data, ingestor data, and media files
    tar -czf "$BACKUP_FILE" \
        -C "$DATA_DIR" \
        --exclude='postgres' \
        --exclude='redis' \
        . 2>/dev/null || true

    if [ -f "$BACKUP_FILE" ] && [ -s "$BACKUP_FILE" ]; then
        SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        log "Files backup created: $BACKUP_FILE ($SIZE)"

        # Create checksum
        sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
    else
        warn "Files backup may be empty or failed"
    fi

    cleanup_old_backups "$BACKUP_DIR/files" "mandari_files_*.tar.gz"
}

# =============================================================================
# Full Backup
# =============================================================================

full_backup() {
    log "Starting full backup..."
    backup_database
    backup_files
    log "Full backup complete!"
}

# =============================================================================
# Cleanup Old Backups
# =============================================================================

cleanup_old_backups() {
    local dir=$1
    local pattern=$2

    log "Cleaning up backups older than $RETENTION_DAYS days..."

    find "$dir" -name "$pattern" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
    find "$dir" -name "*.sha256" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true

    log "Cleanup complete"
}

# =============================================================================
# List Backups
# =============================================================================

list_backups() {
    ensure_backup_dir

    echo ""
    echo "=========================================="
    echo "        Available Backups"
    echo "=========================================="
    echo ""

    echo -e "${BLUE}=== Database Backups ===${NC}"
    if ls -la "$BACKUP_DIR/db/"*.sql.gz 2>/dev/null; then
        :
    else
        echo "  No database backups found"
    fi

    echo ""
    echo -e "${BLUE}=== Files Backups ===${NC}"
    if ls -la "$BACKUP_DIR/files/"*.tar.gz 2>/dev/null; then
        :
    else
        echo "  No files backups found"
    fi

    echo ""
    echo "=========================================="

    # Show disk usage
    echo ""
    echo "Backup disk usage:"
    du -sh "$BACKUP_DIR" 2>/dev/null || echo "  Unable to calculate"
}

# =============================================================================
# Restore
# =============================================================================

restore_database() {
    local backup_file=$1

    if [ ! -f "$backup_file" ]; then
        error "Backup file not found: $backup_file"
    fi

    # Verify checksum if exists
    if [ -f "$backup_file.sha256" ]; then
        log "Verifying backup integrity..."
        if sha256sum -c "$backup_file.sha256"; then
            log "Checksum verified"
        else
            error "Checksum verification failed!"
        fi
    else
        warn "No checksum file found, skipping verification"
    fi

    warn "=== DATABASE RESTORE ==="
    warn "This will OVERWRITE the current database!"
    read -p "Are you sure? Type 'RESTORE' to continue: " confirm
    if [ "$confirm" != "RESTORE" ]; then
        log "Restore cancelled"
        exit 0
    fi

    log "Stopping application containers..."
    docker compose -f /opt/mandari/docker-compose.yml stop api ingestor web-public web-work || true

    log "Restoring database..."
    gunzip -c "$backup_file" | docker exec -i mandari-postgres psql \
        -U "${POSTGRES_USER:-mandari}" \
        -d "${POSTGRES_DB:-mandari}"

    log "Starting application containers..."
    docker compose -f /opt/mandari/docker-compose.yml up -d

    log "Database restore complete!"
}

restore_files() {
    local backup_file=$1

    if [ ! -f "$backup_file" ]; then
        error "Backup file not found: $backup_file"
    fi

    warn "=== FILES RESTORE ==="
    warn "This will OVERWRITE current files!"
    read -p "Are you sure? Type 'RESTORE' to continue: " confirm
    if [ "$confirm" != "RESTORE" ]; then
        log "Restore cancelled"
        exit 0
    fi

    DATA_DIR="${DATA_DIR:-/mnt/data}"

    log "Stopping application containers..."
    docker compose -f /opt/mandari/docker-compose.yml stop meilisearch ingestor || true

    log "Restoring files..."
    tar -xzf "$backup_file" -C "$DATA_DIR"

    log "Starting application containers..."
    docker compose -f /opt/mandari/docker-compose.yml up -d

    log "Files restore complete!"
}

# =============================================================================
# Remote Backup (to S3 or similar)
# =============================================================================

remote_backup() {
    log "Creating remote backup..."

    # Check for S3 credentials
    if [ -z "${S3_BUCKET:-}" ]; then
        warn "S3_BUCKET not set, skipping remote backup"
        return
    fi

    ensure_backup_dir

    # Create fresh backup
    backup_database
    backup_files

    # Upload to S3
    log "Uploading to S3..."
    LATEST_DB=$(ls -t "$BACKUP_DIR/db/"*.sql.gz 2>/dev/null | head -1)
    LATEST_FILES=$(ls -t "$BACKUP_DIR/files/"*.tar.gz 2>/dev/null | head -1)

    if command -v aws &> /dev/null; then
        if [ -n "$LATEST_DB" ]; then
            aws s3 cp "$LATEST_DB" "s3://$S3_BUCKET/backups/db/" --storage-class STANDARD_IA
            aws s3 cp "$LATEST_DB.sha256" "s3://$S3_BUCKET/backups/db/"
        fi

        if [ -n "$LATEST_FILES" ]; then
            aws s3 cp "$LATEST_FILES" "s3://$S3_BUCKET/backups/files/" --storage-class STANDARD_IA
            aws s3 cp "$LATEST_FILES.sha256" "s3://$S3_BUCKET/backups/files/"
        fi

        log "Remote backup complete!"
    else
        warn "AWS CLI not installed, skipping S3 upload"
    fi
}

# =============================================================================
# Help
# =============================================================================

show_help() {
    echo "Mandari 2.0 - Backup Script"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  db           Backup PostgreSQL database"
    echo "  files        Backup uploaded files and search data"
    echo "  full         Full backup (db + files)"
    echo "  list         List available backups"
    echo "  restore-db   Restore database from backup"
    echo "  restore-files Restore files from backup"
    echo "  remote       Create backup and upload to S3"
    echo ""
    echo "Environment Variables:"
    echo "  BACKUP_DIR      Backup directory (default: /opt/mandari/backups)"
    echo "  RETENTION_DAYS  Days to keep backups (default: 30)"
    echo "  S3_BUCKET       S3 bucket for remote backups"
    echo ""
    echo "Examples:"
    echo "  $0 full                      # Create full backup"
    echo "  $0 list                      # List backups"
    echo "  $0 restore-db backup.sql.gz  # Restore database"
}

# =============================================================================
# Main
# =============================================================================

load_config

case "${1:-help}" in
    db)
        backup_database
        ;;
    files)
        backup_files
        ;;
    full)
        full_backup
        ;;
    list)
        list_backups
        ;;
    restore-db)
        restore_database "${2:-}"
        ;;
    restore-files)
        restore_files "${2:-}"
        ;;
    remote)
        remote_backup
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
