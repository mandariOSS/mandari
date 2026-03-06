#!/bin/bash
# =============================================================================
# Mandari - Community Edition Installer
# =============================================================================
# Interactive installer for single-server deployment
#
# Usage:
#   ./install.sh                    # Interactive mode (latest)
#   ./install.sh --tag dev          # Install dev version
#   ./install.sh --tag beta         # Install beta version
#   ./install.sh --tag v1.0.0       # Install specific version
#   ./install.sh --unattended       # Use defaults or environment variables
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

    # System update
    log "Updating system packages..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq
        sudo apt-get upgrade -y -qq
        log "System packages updated"
    elif command -v dnf &>/dev/null; then
        sudo dnf upgrade -y --quiet
        log "System packages updated"
    elif command -v yum &>/dev/null; then
        sudo yum update -y --quiet
        log "System packages updated"
    else
        info "Package manager not detected, skipping system update"
    fi

    # Install Docker if not present
    if ! command -v docker &>/dev/null; then
        log "Docker nicht gefunden — wird automatisch installiert..."
        if command -v curl &>/dev/null; then
            curl -fsSL https://get.docker.com | sh
        elif command -v wget &>/dev/null; then
            wget -qO- https://get.docker.com | sh
        else
            error "Weder curl noch wget verfügbar. Bitte Docker manuell installieren: https://docs.docker.com/engine/install/"
        fi

        # Enable and start Docker
        if command -v systemctl &>/dev/null; then
            systemctl enable docker
            systemctl start docker
        fi

        if ! command -v docker &>/dev/null; then
            error "Docker-Installation fehlgeschlagen. Bitte manuell installieren: https://docs.docker.com/engine/install/"
        fi
        log "Docker erfolgreich installiert"
    fi

    # Check Docker Compose (included in modern Docker)
    if ! docker compose version &>/dev/null; then
        if ! docker-compose --version &>/dev/null; then
            error "Docker Compose nicht verfügbar. Bitte Docker aktualisieren: https://docs.docker.com/compose/install/"
        fi
    fi

    # Check Docker is running
    if ! docker info &>/dev/null; then
        if command -v systemctl &>/dev/null; then
            log "Docker-Daemon wird gestartet..."
            systemctl start docker
            sleep 2
        fi
        if ! docker info &>/dev/null; then
            error "Docker-Daemon läuft nicht. Bitte starten: sudo systemctl start docker"
        fi
    fi

    local docker_version
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    log "Docker version: $docker_version"

    # Check for existing installation
    if [ -f ".env" ]; then
        warn "Existing .env file found!"
        if [ "$UNATTENDED" = "true" ]; then
            log "Overwriting existing configuration (unattended mode)."
        else
            echo ""
            read -p "Overwrite existing configuration? [y/N]: " overwrite
            if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
                log "Keeping existing configuration."
                log "To update, run: ./update.sh"
                exit 0
            fi
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

    # Superuser
    echo ""
    log "Admin-Account erstellen"
    read -p "Admin E-Mail: " ADMIN_EMAIL
    while [ -z "$ADMIN_EMAIL" ]; do
        warn "E-Mail darf nicht leer sein."
        read -p "Admin E-Mail: " ADMIN_EMAIL
    done

    while true; do
        read -sp "Admin Passwort (min. 8 Zeichen): " ADMIN_PASSWORD
        echo ""
        if [ ${#ADMIN_PASSWORD} -lt 8 ]; then
            warn "Passwort muss mindestens 8 Zeichen lang sein."
            continue
        fi
        read -sp "Passwort bestätigen: " admin_password_confirm
        echo ""
        if [ "$ADMIN_PASSWORD" != "$admin_password_confirm" ]; then
            warn "Passwörter stimmen nicht überein."
            continue
        fi
        break
    done

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
    WEBSITE_SECRET_KEY=$(generate_secret 50)
    REDIS_PASSWORD=$(generate_password 32)

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
ACME_EMAIL=${ACME_EMAIL:-noreply@example.com}

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
WEBSITE_SECRET_KEY=${WEBSITE_SECRET_KEY}
REDIS_PASSWORD=${REDIS_PASSWORD}

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
IMAGE_TAG=${IMAGE_TAG}

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

# =============================================================================
# Setup Automatic Daily Backup
# =============================================================================
setup_cron_backup() {
    if [ -x "./backup.sh" ]; then
        mkdir -p "$SCRIPT_DIR/logs"
        local cron_line="0 2 * * * cd $SCRIPT_DIR && ./backup.sh --quiet >> $SCRIPT_DIR/logs/backup.log 2>&1"
        if crontab -l 2>/dev/null | grep -q "mandari.*backup"; then
            info "Tägliches Backup bereits in Crontab eingerichtet"
        else
            (crontab -l 2>/dev/null; echo "$cron_line") | crontab -
            log "Tägliches Backup eingerichtet (2:00 Uhr)"
        fi
    fi
}

# =============================================================================
# Start Services (correct order to avoid race conditions)
# =============================================================================
#
# The ingestor creates oparl_* tables on startup. If it starts before
# Django migrations run, we get "DuplicateTable" errors. So we must:
#   1. Start infrastructure (postgres, redis, meilisearch)
#   2. Start mandari (Django) and run migrations
#   3. THEN start ingestor and caddy
# =============================================================================
start_services() {
    log "Pulling Docker images..."
    docker compose pull

    # --- Phase 1: Infrastructure ---
    log "Starting infrastructure services..."
    docker compose up -d postgres redis meilisearch

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

    # --- Phase 2: Mandari (Django) + Migrations ---
    log "Starting Mandari..."
    docker compose up -d mandari

    echo -n "  Mandari"
    if wait_for_healthy mandari 60; then
        echo -e " ${GREEN}OK${NC}"
    else
        echo -e " ${YELLOW}STARTING${NC}"
        warn "Mandari is taking longer to start. Check logs: docker logs mandari"
    fi

    run_migrations

    # --- Phase 3: Website (Wagtail) + Migrations ---
    log "Starting Marketing Website..."
    docker compose up -d website

    echo -n "  Website"
    if wait_for_healthy mandari-website 60; then
        echo -e " ${GREEN}OK${NC}"
    else
        echo -e " ${YELLOW}STARTING${NC}"
    fi

    run_website_migrations

    # --- Phase 4: Ingestor + Caddy (after migrations) ---
    log "Starting remaining services..."
    docker compose up -d

    echo -n "  Caddy"
    if wait_for_healthy mandari-caddy 60; then
        echo -e " ${GREEN}OK${NC}"
    else
        echo -e " ${YELLOW}STARTING${NC} (SSL-Zertifikat wird erstellt...)"
    fi

    log "All services started"

    # Tägliches Backup um 2:00 Uhr einrichten
    setup_cron_backup
}

# =============================================================================
# Run Migrations
# =============================================================================
run_migrations() {
    log "Running database migrations..."

    if docker exec mandari python manage.py migrate --noinput 2>&1; then
        log "Migrations completed"
    else
        warn "Migration failed. Check logs: docker logs mandari"
        warn "Run manually: docker exec mandari python manage.py migrate"
    fi

    log "Setting up default roles..."
    if docker exec mandari python manage.py setup_roles 2>&1; then
        log "Roles created"
    else
        info "Roles may already exist or setup_roles command not available"
    fi

    log "Configuring Meilisearch indexes..."
    if docker exec mandari python manage.py setup_meilisearch 2>&1; then
        log "Meilisearch indexes configured"
    else
        info "Meilisearch setup skipped or already configured"
    fi

    # Create superuser
    create_superuser
}

# =============================================================================
# Create Superuser
# =============================================================================
create_superuser() {
    if [ -z "${ADMIN_EMAIL:-}" ] || [ -z "${ADMIN_PASSWORD:-}" ]; then
        info "Kein Admin-Account konfiguriert. Erstelle manuell:"
        info "  docker exec -it mandari python manage.py createsuperuser"
        return
    fi

    log "Erstelle Admin-Account..."
    if docker exec \
        -e DJANGO_SUPERUSER_EMAIL="$ADMIN_EMAIL" \
        -e DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
        mandari python manage.py createsuperuser --noinput 2>&1; then
        log "Admin-Account erstellt: ${ADMIN_EMAIL}"
    else
        warn "Admin-Account konnte nicht erstellt werden (existiert evtl. bereits)."
        warn "Erstelle manuell: docker exec -it mandari python manage.py createsuperuser"
    fi

    # Passwort aus dem Speicher löschen
    unset ADMIN_PASSWORD
}

# =============================================================================
# Run Website Migrations
# =============================================================================
run_website_migrations() {
    log "Running website database migrations..."

    if docker exec mandari-website python manage.py migrate --noinput 2>&1; then
        log "Website migrations completed"
    else
        warn "Website migration failed. Check logs: docker logs mandari-website"
    fi
}

# =============================================================================
# Verify Installation
# =============================================================================
verify_installation() {
    log "Verifikation der Installation..."
    echo ""

    local all_ok=true

    # Check each container
    for container in mandari-postgres mandari-redis mandari-meilisearch mandari mandari-website mandari-caddy mandari-ingestor; do
        local status
        local health
        status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
        health=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "none")

        local label
        case "$container" in
            mandari-postgres)   label="PostgreSQL" ;;
            mandari-redis)      label="Redis" ;;
            mandari-meilisearch) label="Meilisearch" ;;
            mandari)            label="Mandari" ;;
            mandari-website)    label="Website" ;;
            mandari-caddy)      label="Caddy" ;;
            mandari-ingestor)   label="Ingestor" ;;
        esac

        printf "  %-14s " "$label"
        if [ "$status" = "running" ]; then
            if [ "$health" = "healthy" ]; then
                echo -e "${GREEN}✓ healthy${NC}"
            elif [ "$health" = "none" ]; then
                echo -e "${GREEN}✓ running${NC}"
            else
                echo -e "${YELLOW}⚠ $health${NC}"
                all_ok=false
            fi
        else
            echo -e "${RED}✗ $status${NC}"
            all_ok=false
        fi
    done

    echo ""

    # Check HTTPS access
    if [ "$DOMAIN" != "localhost" ]; then
        printf "  %-14s " "HTTPS"
        if command -v curl &>/dev/null; then
            local http_code
            http_code=$(curl -sk -o /dev/null -w '%{http_code}' "https://${DOMAIN}/health/" 2>/dev/null || echo "000")
            if [ "$http_code" = "200" ]; then
                echo -e "${GREEN}✓ https://${DOMAIN} erreichbar${NC}"
            elif [ "$http_code" = "000" ]; then
                echo -e "${YELLOW}⚠ Noch nicht erreichbar (SSL-Zertifikat wird erstellt...)${NC}"
                info "Versuche in 30 Sekunden erneut: curl -I https://${DOMAIN}"
            else
                echo -e "${YELLOW}⚠ HTTP $http_code (evtl. noch am Starten)${NC}"
            fi
        else
            echo -e "${YELLOW}⚠ curl nicht installiert, überspringe HTTPS-Test${NC}"
        fi
        echo ""
    fi

    # Show troubleshooting if something is wrong
    if [ "$all_ok" = "false" ]; then
        echo ""
        warn "Einige Services sind noch nicht healthy."
        echo ""
        echo "  Fehlerbehebung:"
        echo "    Logs ansehen:     docker compose logs -f <service>"
        echo "    Neustart:         docker compose restart <service>"
        echo "    Status prüfen:    docker compose ps"
        echo ""
        echo "  Häufige Probleme:"
        echo "    Caddy unhealthy → SSL-Zertifikat wird noch erstellt (Port 80/443 offen?)"
        echo "    Mandari 400     → ALLOWED_HOSTS prüfen in .env"
        echo ""
    fi
}

# =============================================================================
# Show Summary
# =============================================================================
show_summary() {
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Installation abgeschlossen!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""

    if [ "$DOMAIN" != "localhost" ]; then
        echo -e "  URL:        ${BLUE}https://${DOMAIN}${NC}"
    else
        echo -e "  URL:        ${BLUE}http://localhost${NC}"
    fi
    echo -e "  Version:    ${CYAN}${IMAGE_TAG}${NC}"
    echo -e "  Admin:      ${CYAN}${ADMIN_EMAIL:-nicht konfiguriert}${NC}"
    echo ""

    verify_installation

    echo "  Befehle:"
    echo "    Logs ansehen:    docker compose logs -f"
    echo "    Status prüfen:   docker compose ps"
    echo "    Stoppen:         docker compose down"
    echo "    Updaten:         ./update.sh"
    echo "    Backup:          ./backup.sh"
    echo ""
    echo -e "  ${YELLOW}WICHTIG: .env-Datei sicher aufbewahren!${NC}"
    echo -e "  ${YELLOW}         Enthält Verschlüsselungs-Keys.${NC}"
    echo ""

    echo -e "  ${BLUE}Empfohlen: Firewall einrichten${NC}"
    echo "    sudo ufw allow 22/tcp"
    echo "    sudo ufw allow 80/tcp"
    echo "    sudo ufw allow 443/tcp"
    echo "    sudo ufw enable"
    echo ""
}

# =============================================================================
# Main
# =============================================================================
main() {
    UNATTENDED=false
    IMAGE_TAG=""

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --unattended)
                UNATTENDED=true
                shift
                ;;
            --tag)
                if [ -z "${2:-}" ]; then
                    error "--tag requires a value (e.g., --tag dev)"
                fi
                IMAGE_TAG="$2"
                shift 2
                ;;
            --tag=*)
                IMAGE_TAG="${1#--tag=}"
                shift
                ;;
            *)
                error "Unknown argument: $1\nUsage: ./install.sh [--tag dev|beta|latest|VERSION] [--unattended]"
                ;;
        esac
    done

    show_banner
    check_prerequisites

    # Check for unattended mode
    if [ "$UNATTENDED" = "true" ]; then
        DOMAIN="${DOMAIN:-localhost}"
        ACME_EMAIL="${ACME_EMAIL:-}"
        TIMEZONE="${TZ:-Europe/Berlin}"
        POSTGRES_USER="${POSTGRES_USER:-mandari}"
        POSTGRES_DB="${POSTGRES_DB:-mandari}"
        REDIS_MAXMEMORY="${REDIS_MAXMEMORY:-256mb}"
        INGESTOR_INTERVAL="${INGESTOR_INTERVAL:-15}"
        IMAGE_TAG="${IMAGE_TAG:-latest}"
        ADMIN_EMAIL="${ADMIN_EMAIL:-}"
        ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
        log "Running in unattended mode"
    else
        configure_interactively

        # Image tag selection (if not set via --tag)
        if [ -z "$IMAGE_TAG" ]; then
            echo ""
            echo -e "  ${CYAN}Release-Kanal wählen:${NC}"
            echo "    1) latest  — Stabile Version (empfohlen für Produktion)"
            echo "    2) beta    — Beta-Version (für Tests)"
            echo "    3) dev     — Entwicklungsversion (instabil)"
            echo ""
            read -p "  Auswahl [1]: " channel_choice
            case "${channel_choice:-1}" in
                1) IMAGE_TAG="latest" ;;
                2) IMAGE_TAG="beta" ;;
                3) IMAGE_TAG="dev" ;;
                *) IMAGE_TAG="latest" ;;
            esac
        fi
    fi

    log "Image-Tag: $IMAGE_TAG"
    generate_secrets
    create_env_file
    start_services
    show_summary
}

# Run main function
main "$@"
