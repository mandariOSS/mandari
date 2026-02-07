#!/bin/bash
# =============================================================================
# Mandari - Update Script
# =============================================================================
# Updates Mandari to the latest version
#
# Usage:
#   ./update.sh              # Update to latest version
#   ./update.sh v1.2.0       # Update to specific version
#   ./update.sh --no-backup  # Skip backup (not recommended)
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
    echo -e "${GREEN}[MANDARI]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

wait_for_healthy() {
    local container=$1
    local max_attempts=${2:-60}
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")

        if [ "$status" = "healthy" ]; then
            return 0
        fi

        if [ "$status" = "unhealthy" ]; then
            return 1
        fi

        attempt=$((attempt + 1))
        printf "."
        sleep 2
    done

    echo ""
    return 1
}

# =============================================================================
# Parse Arguments
# =============================================================================
TARGET_VERSION=""
SKIP_BACKUP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [VERSION] [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  VERSION       Target version (e.g., v1.2.0). Defaults to 'latest'"
            echo "  --no-backup   Skip creating a backup before updating"
            echo "  -h, --help    Show this help message"
            exit 0
            ;;
        *)
            TARGET_VERSION="$1"
            shift
            ;;
    esac
done

# =============================================================================
# Pre-flight Checks
# =============================================================================
log "Mandari Update"
echo "============================================"

# Check if .env exists
if [ ! -f ".env" ]; then
    error "No .env file found. Is Mandari installed?"
fi

# Check if Docker is running
if ! docker info &>/dev/null; then
    error "Docker daemon is not running"
fi

# Check current status
log "Current service status:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || docker compose ps

# =============================================================================
# Backup
# =============================================================================
if [ "$SKIP_BACKUP" = false ]; then
    echo ""
    warn "It is recommended to create a backup before updating."
    read -p "Create backup now? [Y/n]: " do_backup
    if [[ ! "$do_backup" =~ ^[Nn]$ ]]; then
        if [ -x "./backup.sh" ]; then
            log "Creating backup..."
            ./backup.sh
        else
            warn "backup.sh not found or not executable. Skipping backup."
        fi
    fi
else
    warn "Skipping backup (--no-backup flag)"
fi

# =============================================================================
# Update Configuration
# =============================================================================
if [ -n "$TARGET_VERSION" ]; then
    log "Target version: $TARGET_VERSION"
    # Update IMAGE_TAG in .env
    if grep -q "^IMAGE_TAG=" .env; then
        sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=$TARGET_VERSION/" .env
    else
        echo "IMAGE_TAG=$TARGET_VERSION" >> .env
    fi
else
    log "Target version: latest"
    if grep -q "^IMAGE_TAG=" .env; then
        sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=latest/" .env
    fi
fi

# =============================================================================
# Git Pull (if cloned from repo)
# =============================================================================
if [ -d ".git" ]; then
    log "Updating from Git repository..."
    git fetch origin

    # Check if we're on a branch
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ -n "$current_branch" ] && [ "$current_branch" != "HEAD" ]; then
        git pull origin "$current_branch" || warn "Git pull failed. Continuing with Docker update."
    fi
fi

# =============================================================================
# Pull New Images
# =============================================================================
echo ""
log "Pulling new Docker images..."
docker compose pull

# =============================================================================
# Restart Services (correct order for migrations)
# =============================================================================
# Stop all services first
log "Stopping services..."
docker compose down

# Phase 1: Start infrastructure
log "Starting infrastructure services..."
docker compose up -d postgres redis meilisearch

echo -n "  PostgreSQL"
if wait_for_healthy mandari-postgres 30; then
    echo -e " ${GREEN}OK${NC}"
else
    echo -e " ${YELLOW}WAITING${NC}"
fi

echo -n "  Redis"
if wait_for_healthy mandari-redis 15; then
    echo -e " ${GREEN}OK${NC}"
else
    echo -e " ${YELLOW}WAITING${NC}"
fi

echo -n "  Meilisearch"
if wait_for_healthy mandari-meilisearch 30; then
    echo -e " ${GREEN}OK${NC}"
else
    echo -e " ${YELLOW}WAITING${NC}"
fi

# Phase 2: Start mandari + run migrations
log "Starting Mandari..."
docker compose up -d mandari

echo -n "  Mandari"
if wait_for_healthy mandari 60; then
    echo -e " ${GREEN}OK${NC}"
else
    echo -e " ${YELLOW}STARTING${NC}"
fi

log "Running database migrations..."
if docker exec mandari python manage.py migrate --noinput 2>&1; then
    log "Migrations completed successfully"
else
    warn "Migration failed or not needed. Check logs: docker logs mandari"
fi

# Phase 3: Start website (Wagtail) + run migrations
log "Starting Marketing Website..."
docker compose up -d website

echo -n "  Website"
if wait_for_healthy mandari-website 60; then
    echo -e " ${GREEN}OK${NC}"
else
    echo -e " ${YELLOW}STARTING${NC}"
fi

log "Running website database migrations..."
if docker exec mandari-website python manage.py migrate --noinput 2>&1; then
    log "Website migrations completed"
else
    warn "Website migration failed or not needed. Check logs: docker logs mandari-website"
fi

# Phase 4: Start ingestor + caddy (after migrations)
log "Starting remaining services..."
docker compose up -d

# =============================================================================
# Verify
# =============================================================================
log "Verifying services..."
sleep 5

echo ""
log "Service status after update:"
docker compose ps

# =============================================================================
# Health Check
# =============================================================================
echo ""
log "Health Check..."
if docker exec mandari curl -sf http://localhost:8000/health &>/dev/null; then
    echo -e "  Mandari:  ${GREEN}healthy${NC}"
else
    echo -e "  Mandari:  ${YELLOW}starting...${NC}"
    warn "Mandari is still starting. Check logs: docker logs mandari -f"
fi

if docker exec mandari-website curl -sf http://localhost:8001/health/ &>/dev/null; then
    echo -e "  Website:  ${GREEN}healthy${NC}"
else
    echo -e "  Website:  ${YELLOW}starting...${NC}"
fi

# =============================================================================
# Cleanup
# =============================================================================
echo ""
read -p "Remove old Docker images to free disk space? [y/N]: " cleanup
if [[ "$cleanup" =~ ^[Yy]$ ]]; then
    log "Cleaning up old images..."
    docker image prune -f
fi

# =============================================================================
# Done
# =============================================================================
echo ""
log "Update completed!"
echo ""
echo "  View logs:     docker compose logs -f"
echo "  Check status:  docker compose ps"
echo ""
