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
# Restart Services
# =============================================================================
log "Restarting services with new images..."
docker compose up -d

# =============================================================================
# Run Migrations
# =============================================================================
log "Waiting for services to start..."
sleep 10

log "Running database migrations..."
if docker exec mandari-api python manage.py migrate --noinput 2>/dev/null; then
    log "Migrations completed successfully"
else
    warn "Migration failed or not needed. Check logs: docker logs mandari-api"
fi

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
log "Checking API health..."
if docker exec mandari-api curl -sf http://localhost:8000/health &>/dev/null; then
    echo -e "  API: ${GREEN}healthy${NC}"
else
    echo -e "  API: ${YELLOW}starting...${NC}"
    warn "API is still starting. Check logs: docker logs mandari-api -f"
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
