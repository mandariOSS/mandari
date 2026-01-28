#!/bin/bash
# Mandari Staging Environment - Start Script (Linux/macOS)
# ========================================================
#
# This script sets up and starts the Mandari staging environment.
#
# Prerequisites:
# - Docker and Docker Compose installed
# - sudo access (for hosts file modification)
#
# Usage:
#   ./start-staging.sh           # Start environment
#   ./start-staging.sh --build   # Rebuild and start
#   ./start-staging.sh --down    # Stop environment
#   ./start-staging.sh --logs    # Show logs
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info() { echo -e "${CYAN}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Banner
echo ""
echo -e "${BLUE}  __  __                 _            _ ${NC}"
echo -e "${BLUE} |  \\/  | __ _ _ __   __| | __ _ _ __(_)${NC}"
echo -e "${BLUE} | |\\/| |/ _\` | '_ \\ / _\` |/ _\` | '__| |${NC}"
echo -e "${BLUE} | |  | | (_| | | | | (_| | (_| | |  | |${NC}"
echo -e "${BLUE} |_|  |_|\\__,_|_| |_|\\__,_|\\__,_|_|  |_|${NC}"
echo ""
echo -e " Staging Environment - mandari.dev"
echo " ===================================="
echo ""

# Parse arguments
BUILD=false
DOWN=false
LOGS=false
SKIP_HOSTS=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --build|-b)
            BUILD=true
            shift
            ;;
        --down|-d)
            DOWN=true
            shift
            ;;
        --logs|-l)
            LOGS=true
            shift
            ;;
        --skip-hosts)
            SKIP_HOSTS=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Handle --down flag
if [ "$DOWN" = true ]; then
    info "Stopping staging environment..."
    docker-compose -f docker-compose.staging.yml down
    success "Staging environment stopped."
    exit 0
fi

# Handle --logs flag
if [ "$LOGS" = true ]; then
    info "Showing logs (Ctrl+C to exit)..."
    docker-compose -f docker-compose.staging.yml logs -f
    exit 0
fi

# Step 1: Check Docker
info "Checking Docker..."
if ! docker info > /dev/null 2>&1; then
    error "Docker is not running. Please start Docker."
    exit 1
fi
success "Docker is running."

# Step 2: Add hosts file entry
if [ "$SKIP_HOSTS" = false ]; then
    info "Checking hosts file entry for mandari.dev..."
    if ! grep -q "mandari.dev" /etc/hosts; then
        warn "Adding mandari.dev to hosts file (requires sudo)..."
        echo "127.0.0.1 mandari.dev" | sudo tee -a /etc/hosts > /dev/null
        success "Added mandari.dev to hosts file."
    else
        success "mandari.dev already in hosts file."
    fi
fi

# Step 3: Create .env file if not exists
if [ ! -f ".env" ]; then
    info "Creating .env from template..."
    cp .env.staging .env
    success ".env file created. Please review and adjust settings."
fi

# Step 4: Build images if requested
if [ "$BUILD" = true ]; then
    info "Building Docker images..."
    docker-compose -f docker-compose.staging.yml build
    success "Images built successfully."
fi

# Step 5: Start containers
info "Starting staging environment..."
docker-compose -f docker-compose.staging.yml up -d

# Step 6: Wait for services
info "Waiting for services to be ready..."
max_retries=30
retry=0

while [ $retry -lt $max_retries ]; do
    if docker-compose -f docker-compose.staging.yml exec -T django curl -s http://localhost:8000/health/ 2>/dev/null | grep -q "ok"; then
        break
    fi
    retry=$((retry + 1))
    echo -n "."
    sleep 2
done
echo ""

if [ $retry -ge $max_retries ]; then
    warn "Services may not be fully ready. Check logs with: ./start-staging.sh --logs"
else
    success "All services are ready!"
fi

# Step 7: Show status
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN} Mandari Staging Environment is running!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo " Web Application:  http://mandari.dev"
echo " Django Admin:     http://mandari.dev/admin/"
echo -e " pgAdmin:          http://localhost:5050 ${YELLOW}(use --profile tools)${NC}"
echo ""
echo -e "${YELLOW} Commands:${NC}"
echo "   Stop:    ./start-staging.sh --down"
echo "   Logs:    ./start-staging.sh --logs"
echo "   Rebuild: ./start-staging.sh --build"
echo ""
