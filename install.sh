#!/bin/bash
# =============================================================================
# Mandari - Community Edition Installer
# =============================================================================
# Interactive installer for single-server deployment
#
# Usage:
#   ./install.sh              # Interactive mode
#   ./install.sh --unattended # Use defaults or environment variables
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

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

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Generate secure random string
generate_secret() {
    local length=${1:-32}
    if command -v openssl &>/dev/null; then
        openssl rand -base64 "$length" 2>/dev/null | tr -d '\n'
    else
        head -c "$length" /dev/urandom 2>/dev/null | base64 | tr -d '\n'
    fi
}

# Generate password-safe string (alphanumeric only)
generate_password() {
    local length=${1:-32}
    generate_secret 48 | tr -dc 'a-zA-Z0-9' | head -c "$length"
}

# =============================================================================
# Banner
# =============================================================================
show_banner() {
    echo -e "${CYAN}"
    cat << "EOF"
  __  __                 _            _
 |  \/  | __ _ _ __   __| | __ _ _ __(_)
 | |\/| |/ _` | '_ \ / _` |/ _` | '__| |
 | |  | | (_| | | | | (_| | (_| | |  | |
 |_|  |_|\__,_|_| |_|\__,_|\__,_|_|  |_|

 Kommunalpolitische Transparenz fuer Deutschland
 Open Source unter AGPL-3.0

EOF
    echo -e "${NC}"
}

# =============================================================================
# Prerequisites Check
# =============================================================================
check_prerequisites() {
    log "Checking prerequisites..."

    # Check if running as root (warn, don't require)
    if [ "$EUID" -eq 0 ]; then
        warn "Running as root. Consider using a non-root user with Docker group access."
    fi

    # Check Docker
    if ! command -v docker &>/dev/null; then
        error "Docker is not installed. Please install Docker first: https://docs.docker.com/engine/install/"
    fi

    # Check Docker Compose
    if ! docker compose version &>/dev/null; then
        if ! docker-compose --version &>/dev/null; then
            error "Docker Compose is not installed. Please install Docker Compose: https://docs.docker.com/compose/install/"
        fi
    fi

    # Check Docker is running
    if ! docker info &>/dev/null; then
        error "Docker daemon is not running. Please start Docker."
    fi

    local docker_version
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    log "Docker version: $docker_version"

    # Check for existing installation
    if [ -f ".env" ]; then
        warn "Existing .env file found!"
        echo ""
        read -p "Overwrite existing configuration? [y/N]: " overwrite
        if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
            log "Keeping existing configuration."
            log "To update, run: ./update.sh"
            exit 0
        fi
    fi

    log "Prerequisites check passed"
}

# =============================================================================
# Interactive Configuration
# =============================================================================
configure_interactively() {
    echo ""
    log "Configuration"
    echo "============================================"
    echo ""

    # Domain
    read -p "Domain (e.g., mandari.example.com) [localhost]: " input_domain
    DOMAIN="${input_domain:-localhost}"

    # Email for Let's Encrypt
    ACME_EMAIL=""
    if [ "$DOMAIN" != "localhost" ]; then
        read -p "Email for SSL certificate (Let's Encrypt): " input_email
        ACME_EMAIL="$input_email"

        if [ -z "$ACME_EMAIL" ]; then
            warn "No email provided. SSL certificates may fail to renew."
        fi
    fi

    # Timezone
    read -p "Timezone [Europe/Berlin]: " input_tz
    TIMEZONE="${input_tz:-Europe/Berlin}"

    # Advanced options
    echo ""
    read -p "Configure advanced options? [y/N]: " advanced
    if [[ "$advanced" =~ ^[Yy]$ ]]; then
        read -p "PostgreSQL user [mandari]: " input_pg_user
        POSTGRES_USER="${input_pg_user:-mandari}"

        read -p "PostgreSQL database [mandari]: " input_pg_db
        POSTGRES_DB="${input_pg_db:-mandari}"

        read -p "Redis max memory [256mb]: " input_redis_mem
        REDIS_MAXMEMORY="${input_redis_mem:-256mb}"

        read -p "OParl sync interval in minutes [15]: " input_sync_interval
        INGESTOR_INTERVAL="${input_sync_interval:-15}"
    else
        POSTGRES_USER="mandari"
        POSTGRES_DB="mandari"
        REDIS_MAXMEMORY="256mb"
        INGESTOR_INTERVAL="15"
    fi
}

# =============================================================================
# Generate Secrets
# =============================================================================
generate_secrets() {
    log "Generating secure keys..."

    SECRET_KEY=$(generate_secret 50)
    POSTGRES_PASSWORD=$(generate_password 32)
    MEILISEARCH_KEY=$(generate_secret 32)
    ENCRYPTION_MASTER_KEY=$(generate_secret 32)

    log "Secure keys generated"
}

# =============================================================================
# Create Environment File
# =============================================================================
create_env_file() {
    log "Creating configuration file..."

    cat > .env << EOF
# =============================================================================
# Mandari - Configuration
# Generated: $(date)
# =============================================================================
# WARNING: This file contains secrets. Keep it secure!
#          Do not commit to version control!
# =============================================================================

# =============================================================================
# Domain & SSL
# =============================================================================
DOMAIN=${DOMAIN}
ACME_EMAIL=${ACME_EMAIL}

# =============================================================================
# Timezone
# =============================================================================
TZ=${TIMEZONE}

# =============================================================================
# Database (PostgreSQL)
# =============================================================================
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}

# =============================================================================
# Security (DO NOT MODIFY AFTER INSTALLATION!)
# =============================================================================
# Changing these keys will make existing encrypted data unreadable
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_MASTER_KEY=${ENCRYPTION_MASTER_KEY}
MEILISEARCH_KEY=${MEILISEARCH_KEY}

# =============================================================================
# Resources
# =============================================================================
REDIS_MAXMEMORY=${REDIS_MAXMEMORY}

# =============================================================================
# OParl Ingestor
# =============================================================================
INGESTOR_INTERVAL=${INGESTOR_INTERVAL}
INGESTOR_FULL_SYNC_HOUR=3
INGESTOR_CONCURRENT=10

# =============================================================================
# Version
# =============================================================================
IMAGE_TAG=latest

# =============================================================================
# Optional: Email Configuration
# =============================================================================
# Uncomment and configure to enable email notifications
# EMAIL_HOST=smtp.example.com
# EMAIL_PORT=587
# EMAIL_HOST_USER=
# EMAIL_HOST_PASSWORD=
# EMAIL_USE_TLS=true
# DEFAULT_FROM_EMAIL=noreply@example.com
EOF

    # Secure the file
    chmod 600 .env

    log "Configuration saved to .env"
}

# =============================================================================
# Start Services
# =============================================================================
start_services() {
    log "Pulling Docker images..."
    docker compose pull

    log "Starting Mandari services..."
    docker compose up -d

    log "Waiting for services to be healthy..."
}

# =============================================================================
# Wait for Health
# =============================================================================
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
            warn "Container $container is unhealthy"
            return 1
        fi

        attempt=$((attempt + 1))
        printf "."
        sleep 2
    done

    echo ""
    return 1
}

wait_for_all_services() {
    echo -n "  PostgreSQL"
    if wait_for_healthy mandari-postgres 30; then
        echo -e " ${GREEN}OK${NC}"
    else
        echo -e " ${YELLOW}WAITING${NC}"
    fi

    echo -n "  Redis"
    if wait_for_healthy mandari-redis 30; then
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

    echo -n "  API"
    if wait_for_healthy mandari-api 60; then
        echo -e " ${GREEN}OK${NC}"
    else
        echo -e " ${YELLOW}STARTING${NC}"
        warn "API is taking longer to start. Check logs: docker logs mandari-api"
    fi
}

# =============================================================================
# Run Migrations
# =============================================================================
run_migrations() {
    log "Running database migrations..."

    # Wait a bit more for API to be fully ready
    sleep 5

    if docker exec mandari-api python manage.py migrate --noinput 2>/dev/null; then
        log "Migrations completed"
    else
        warn "Migration failed. The API may not be ready yet."
        warn "Run manually: docker exec mandari-api python manage.py migrate"
    fi

    log "Setting up default roles..."
    if docker exec mandari-api python manage.py setup_roles 2>/dev/null; then
        log "Roles created"
    else
        info "Roles may already exist or setup_roles command not available"
    fi
}

# =============================================================================
# Show Summary
# =============================================================================
show_summary() {
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Installation Complete!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""

    if [ "$DOMAIN" != "localhost" ]; then
        echo -e "  URL:        ${BLUE}https://${DOMAIN}${NC}"
    else
        echo -e "  URL:        ${BLUE}http://localhost${NC}"
    fi

    echo ""
    echo "  Commands:"
    echo "    View logs:       docker compose logs -f"
    echo "    Check status:    docker compose ps"
    echo "    Stop:            docker compose down"
    echo "    Update:          ./update.sh"
    echo "    Backup:          ./backup.sh"
    echo ""
    echo -e "  ${YELLOW}IMPORTANT: Back up the .env file securely!${NC}"
    echo -e "  ${YELLOW}           It contains your encryption keys.${NC}"
    echo ""

    if [ "$DOMAIN" != "localhost" ]; then
        info "SSL certificate will be obtained automatically on first request."
        info "Make sure your domain DNS points to this server."
    fi

    echo ""
    docker compose ps
}

# =============================================================================
# Main
# =============================================================================
main() {
    show_banner
    check_prerequisites

    # Check for unattended mode
    if [ "${1:-}" = "--unattended" ]; then
        DOMAIN="${DOMAIN:-localhost}"
        ACME_EMAIL="${ACME_EMAIL:-}"
        TIMEZONE="${TZ:-Europe/Berlin}"
        POSTGRES_USER="${POSTGRES_USER:-mandari}"
        POSTGRES_DB="${POSTGRES_DB:-mandari}"
        REDIS_MAXMEMORY="${REDIS_MAXMEMORY:-256mb}"
        INGESTOR_INTERVAL="${INGESTOR_INTERVAL:-15}"
        log "Running in unattended mode"
    else
        configure_interactively
    fi

    generate_secrets
    create_env_file
    start_services
    wait_for_all_services
    run_migrations
    show_summary
}

# Run main function
main "$@"
