#!/bin/bash
# =============================================================================
# Mandari - PostgreSQL Replica Initialization Script
# =============================================================================
# This script initializes a PostgreSQL hot standby replica using pg_basebackup.
# Run this on the SLAVE server BEFORE starting the PostgreSQL container.
#
# Prerequisites:
# - Master PostgreSQL must be running and accessible
# - Master must have replication configured (wal_level=replica)
# - Network connectivity between slave and master (10.0.0.3:5432)
#
# Usage: ./init-replica.sh
# =============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[REPLICA]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# =============================================================================
# Configuration
# =============================================================================
MASTER_IP="${MASTER_PRIVATE_IP:-10.0.0.3}"
MASTER_PORT="${MASTER_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-mandari}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD}"
DATA_DIR="${DATA_DIR:-/mnt/data}/postgres"
APP_DIR="${APP_DIR:-/opt/mandari}"

# =============================================================================
# Pre-flight checks
# =============================================================================
log "PostgreSQL Replica Initialization"
echo "============================================"
echo "Master: ${MASTER_IP}:${MASTER_PORT}"
echo "Data Directory: ${DATA_DIR}"
echo "============================================"

# Check if password is set
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    # Try to load from .env file
    if [ -f "${APP_DIR}/.env" ]; then
        log "Loading credentials from ${APP_DIR}/.env"
        export $(grep -E '^POSTGRES_' "${APP_DIR}/.env" | xargs)
        POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
    fi
fi

[ -z "${POSTGRES_PASSWORD:-}" ] && error "POSTGRES_PASSWORD not set. Export it or ensure .env file exists."

# Check connectivity to master
log "Testing connection to master..."
if ! nc -z -w5 "${MASTER_IP}" "${MASTER_PORT}" 2>/dev/null; then
    error "Cannot connect to master at ${MASTER_IP}:${MASTER_PORT}"
fi
log "Master is reachable"

# =============================================================================
# Stop PostgreSQL container if running
# =============================================================================
log "Stopping PostgreSQL container if running..."
docker stop mandari-postgres 2>/dev/null || true
docker rm mandari-postgres 2>/dev/null || true

# =============================================================================
# Clear existing data directory
# =============================================================================
if [ -d "${DATA_DIR}" ] && [ "$(ls -A ${DATA_DIR} 2>/dev/null)" ]; then
    warn "Data directory is not empty: ${DATA_DIR}"
    read -p "Clear existing data and reinitialize? [y/N]: " confirm
    if [ "${confirm}" != "y" ] && [ "${confirm}" != "Y" ]; then
        error "Aborted by user"
    fi
    log "Clearing data directory..."
    rm -rf "${DATA_DIR:?}"/*
fi

# Ensure directory exists with correct permissions
mkdir -p "${DATA_DIR}"
chown -R 999:999 "${DATA_DIR}" 2>/dev/null || true  # postgres user in container

# =============================================================================
# Run pg_basebackup
# =============================================================================
log "Starting pg_basebackup from master..."
log "This may take several minutes depending on database size..."

# Use a temporary PostgreSQL container to run pg_basebackup
docker run --rm \
    --network host \
    -v "${DATA_DIR}:/var/lib/postgresql/data" \
    -e PGPASSWORD="${POSTGRES_PASSWORD}" \
    postgres:16-alpine \
    pg_basebackup \
        -h "${MASTER_IP}" \
        -p "${MASTER_PORT}" \
        -U "${POSTGRES_USER}" \
        -D /var/lib/postgresql/data \
        -Fp \
        -Xs \
        -P \
        -R

if [ $? -ne 0 ]; then
    error "pg_basebackup failed!"
fi

log "pg_basebackup completed successfully"

# =============================================================================
# Create standby.signal (PostgreSQL 12+ uses this instead of recovery.conf)
# =============================================================================
log "Creating standby.signal file..."
touch "${DATA_DIR}/standby.signal"

# =============================================================================
# Verify setup
# =============================================================================
log "Verifying replica setup..."

if [ ! -f "${DATA_DIR}/standby.signal" ]; then
    error "standby.signal not found!"
fi

if [ ! -f "${DATA_DIR}/PG_VERSION" ]; then
    error "PG_VERSION not found - pg_basebackup may have failed!"
fi

PG_VERSION=$(cat "${DATA_DIR}/PG_VERSION")
log "PostgreSQL version: ${PG_VERSION}"

# =============================================================================
# Set permissions
# =============================================================================
log "Setting permissions..."
chown -R 999:999 "${DATA_DIR}" 2>/dev/null || true

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Replica initialization complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Start the PostgreSQL container:"
echo "     cd ${APP_DIR} && docker compose up -d postgres"
echo ""
echo "  2. Verify replication status on MASTER:"
echo "     docker exec mandari-postgres psql -U ${POSTGRES_USER} -c 'SELECT * FROM pg_stat_replication;'"
echo ""
echo "  3. Verify replica status on SLAVE:"
echo "     docker exec mandari-postgres psql -U ${POSTGRES_USER} -c 'SELECT pg_is_in_recovery();'"
echo "     (Should return 't' for true)"
echo ""
