#!/bin/bash
# =============================================================================
# MANDARI PRODUCTION SETUP
# =============================================================================
# Vorkonfiguriert für Hetzner Server
# =============================================================================

set -euo pipefail

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
section() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

# =============================================================================
# DEINE SERVER-KONFIGURATION
# =============================================================================

# Load Balancer
LB_PUBLIC_IP="91.98.3.147"
LB_PRIVATE_IP="10.0.0.2"

# Master Server
MASTER_PUBLIC_IP="46.225.61.128"
MASTER_PRIVATE_IP="10.0.0.3"

# Slave Server
SLAVE_PUBLIC_IP="46.225.58.145"
SLAVE_PRIVATE_IP="10.0.0.4"

# Domain
DOMAIN="mandari.de"

# SSH Einstellungen
SSH_USER="root"
SSH_KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"

# =============================================================================
# GENERIERTE SECRETS (werden beim ersten Lauf erstellt)
# =============================================================================

SECRETS_FILE="$(dirname "$0")/mandari-secrets.env"

generate_secrets() {
    if [[ -f "$SECRETS_FILE" ]]; then
        source "$SECRETS_FILE"
        log "Secrets aus $SECRETS_FILE geladen"
    else
        info "Generiere neue Secrets..."
        POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
        REPLICATION_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
        SECRET_KEY=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-48)
        ENCRYPTION_MASTER_KEY=$(openssl rand -base64 32)
        MEILISEARCH_KEY=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)

        cat > "$SECRETS_FILE" << EOF
# Mandari Secrets - Generated $(date)
# WICHTIG: Diese Datei sicher aufbewahren!

POSTGRES_PASSWORD="${POSTGRES_PASSWORD}"
REPLICATION_PASSWORD="${REPLICATION_PASSWORD}"
SECRET_KEY="${SECRET_KEY}"
ENCRYPTION_MASTER_KEY="${ENCRYPTION_MASTER_KEY}"
MEILISEARCH_KEY="${MEILISEARCH_KEY}"
EOF
        chmod 600 "$SECRETS_FILE"
        log "Secrets generiert und gespeichert in: $SECRETS_FILE"
    fi
}

# =============================================================================
# SSH FUNKTIONEN
# =============================================================================

ssh_cmd() {
    local host=$1
    shift
    ssh -i "$SSH_KEY" $SSH_OPTS "${SSH_USER}@${host}" "$@"
}

scp_to() {
    local src=$1
    local host=$2
    local dest=$3
    scp -i "$SSH_KEY" $SSH_OPTS "$src" "${SSH_USER}@${host}:${dest}"
}

test_ssh() {
    local host=$1
    local name=$2
    if ssh_cmd "$host" "echo 'OK'" > /dev/null 2>&1; then
        log "SSH zu $name ($host) OK"
        return 0
    else
        error "Keine SSH-Verbindung zu $name ($host)"
        return 1
    fi
}

# =============================================================================
# SERVER BASIS-SETUP
# =============================================================================

setup_server_base() {
    local host=$1
    local name=$2

    section "Basis-Setup: $name"

    info "System aktualisieren..."
    ssh_cmd "$host" "DEBIAN_FRONTEND=noninteractive apt-get update -qq"
    ssh_cmd "$host" "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq"

    info "Pakete installieren..."
    ssh_cmd "$host" "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        curl gnupg lsb-release ca-certificates \
        ufw fail2ban htop vim git jq \
        apt-transport-https software-properties-common"

    info "Docker installieren..."
    ssh_cmd "$host" '
        if ! command -v docker &> /dev/null; then
            curl -fsSL https://get.docker.com | sh
            systemctl enable docker
            systemctl start docker
        fi
        docker --version
    '

    info "Docker Compose Plugin prüfen..."
    ssh_cmd "$host" '
        if ! docker compose version &> /dev/null; then
            apt-get install -y docker-compose-plugin
        fi
        docker compose version
    '

    info "Deploy User erstellen..."
    ssh_cmd "$host" '
        if ! id deploy &> /dev/null; then
            useradd --create-home --shell /bin/bash deploy
            usermod -aG docker deploy
        fi
    '

    info "Verzeichnisse erstellen..."
    ssh_cmd "$host" '
        mkdir -p /opt/mandari/{config,data}
        mkdir -p /opt/mandari/data/{postgres,redis,meilisearch,media,caddy}
        chown -R deploy:deploy /opt/mandari
    '

    info "Firewall konfigurieren..."
    ssh_cmd "$host" '
        ufw --force reset > /dev/null
        ufw default deny incoming
        ufw default allow outgoing
        ufw allow ssh
        ufw allow http
        ufw allow https
        ufw allow from 10.0.0.0/16 comment "Private Network"
        ufw --force enable
    '

    log "$name Basis-Setup abgeschlossen!"
}

# =============================================================================
# DOCKER COMPOSE FÜR MASTER
# =============================================================================

create_master_compose() {
    section "Docker Compose für Master erstellen"

    local tmpfile=$(mktemp)
    cat > "$tmpfile" << 'EOF'
version: "3.9"

services:
  caddy:
    image: caddy:2-alpine
    container_name: mandari-caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./config/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./data/caddy:/data
    networks:
      - mandari
    depends_on:
      api:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    container_name: mandari-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: mandari
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: mandari
    command:
      - "postgres"
      - "-c"
      - "max_connections=200"
      - "-c"
      - "shared_buffers=256MB"
      - "-c"
      - "wal_level=replica"
      - "-c"
      - "max_wal_senders=3"
      - "-c"
      - "listen_addresses=*"
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mandari"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: mandari-redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - ./data/redis:/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  meilisearch:
    image: getmeili/meilisearch:v1.6
    container_name: mandari-meilisearch
    restart: unless-stopped
    environment:
      MEILI_ENV: production
      MEILI_MASTER_KEY: ${MEILISEARCH_KEY}
      MEILI_NO_ANALYTICS: "true"
    volumes:
      - ./data/meilisearch:/meili_data
    networks:
      - mandari
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:7700/health"]
      interval: 30s
      timeout: 10s
      retries: 5

  api:
    image: ghcr.io/mandarioss/mandari-api:${IMAGE_TAG:-latest}
    container_name: mandari-api
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://mandari:${POSTGRES_PASSWORD}@postgres:5432/mandari
      REDIS_URL: redis://redis:6379
      MEILISEARCH_URL: http://meilisearch:7700
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
      SECRET_KEY: ${SECRET_KEY}
      ENCRYPTION_MASTER_KEY: ${ENCRYPTION_MASTER_KEY}
      SITE_URL: https://${DOMAIN}
      DEBUG: "false"
      ALLOWED_HOSTS: ${DOMAIN},localhost,127.0.0.1
    volumes:
      - ./data/media:/app/media
    networks:
      - mandari
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health/"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s

  ingestor:
    image: ghcr.io/mandarioss/mandari-ingestor:${IMAGE_TAG:-latest}
    container_name: mandari-ingestor
    restart: unless-stopped
    command: ["python", "-m", "src.main", "daemon", "--interval", "15", "--metrics-port", "9090"]
    environment:
      DATABASE_URL: postgresql+asyncpg://mandari:${POSTGRES_PASSWORD}@postgres:5432/mandari
      REDIS_URL: redis://redis:6379
      MEILISEARCH_URL: http://meilisearch:7700
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
    ports:
      - "9090:9090"
    networks:
      - mandari
    depends_on:
      postgres:
        condition: service_healthy

networks:
  mandari:
    driver: bridge
EOF

    scp_to "$tmpfile" "$MASTER_PUBLIC_IP" "/opt/mandari/docker-compose.yml"
    rm "$tmpfile"

    log "Docker Compose für Master erstellt!"
}

# =============================================================================
# DOCKER COMPOSE FÜR SLAVE
# =============================================================================

create_slave_compose() {
    section "Docker Compose für Slave erstellen"

    local tmpfile=$(mktemp)
    cat > "$tmpfile" << 'EOF'
version: "3.9"

services:
  caddy:
    image: caddy:2-alpine
    container_name: mandari-caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./config/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./data/caddy:/data
    networks:
      - mandari
    depends_on:
      api:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    container_name: mandari-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: mandari
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: mandari
    command:
      - "postgres"
      - "-c"
      - "max_connections=200"
      - "-c"
      - "shared_buffers=256MB"
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mandari"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: mandari-redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - ./data/redis:/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  meilisearch:
    image: getmeili/meilisearch:v1.6
    container_name: mandari-meilisearch
    restart: unless-stopped
    environment:
      MEILI_ENV: production
      MEILI_MASTER_KEY: ${MEILISEARCH_KEY}
      MEILI_NO_ANALYTICS: "true"
    volumes:
      - ./data/meilisearch:/meili_data
    networks:
      - mandari

  api:
    image: ghcr.io/mandarioss/mandari-api:${IMAGE_TAG:-latest}
    container_name: mandari-api
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://mandari:${POSTGRES_PASSWORD}@postgres:5432/mandari
      REDIS_URL: redis://redis:6379
      MEILISEARCH_URL: http://meilisearch:7700
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
      SECRET_KEY: ${SECRET_KEY}
      ENCRYPTION_MASTER_KEY: ${ENCRYPTION_MASTER_KEY}
      SITE_URL: https://${DOMAIN}
      DEBUG: "false"
      ALLOWED_HOSTS: ${DOMAIN},localhost,127.0.0.1
    volumes:
      - ./data/media:/app/media
    networks:
      - mandari
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health/"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s

networks:
  mandari:
    driver: bridge
EOF

    scp_to "$tmpfile" "$SLAVE_PUBLIC_IP" "/opt/mandari/docker-compose.yml"
    rm "$tmpfile"

    log "Docker Compose für Slave erstellt!"
}

# =============================================================================
# KONFIGURATIONSDATEIEN
# =============================================================================

create_configs() {
    local host=$1
    local name=$2

    section "Konfigurationsdateien für $name"

    # Caddyfile
    local caddyfile=$(mktemp)
    cat > "$caddyfile" << EOF
{
    email admin@${DOMAIN}
    acme_ca https://acme-v02.api.letsencrypt.org/directory
}

${DOMAIN} {
    reverse_proxy api:8000

    header {
        X-Content-Type-Options nosniff
        X-Frame-Options SAMEORIGIN
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }

    encode gzip

    log {
        output file /data/access.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}

# Health Check für Load Balancer
:80 {
    @health path /health
    respond @health "OK" 200

    # Redirect alles andere zu HTTPS
    redir https://{host}{uri} permanent
}
EOF
    scp_to "$caddyfile" "$host" "/opt/mandari/config/Caddyfile"
    rm "$caddyfile"

    # .env Datei
    local envfile=$(mktemp)
    cat > "$envfile" << EOF
# Mandari Environment - ${name}
# Generated: $(date)

DOMAIN=${DOMAIN}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_MASTER_KEY=${ENCRYPTION_MASTER_KEY}
MEILISEARCH_KEY=${MEILISEARCH_KEY}
IMAGE_TAG=latest
EOF
    scp_to "$envfile" "$host" "/opt/mandari/.env"
    rm "$envfile"

    ssh_cmd "$host" "chmod 600 /opt/mandari/.env"

    log "Konfigurationsdateien für $name erstellt!"
}

# =============================================================================
# SERVICES STARTEN
# =============================================================================

start_services() {
    local host=$1
    local name=$2

    section "Services starten: $name"

    info "Docker Images pullen (kann dauern)..."
    ssh_cmd "$host" "cd /opt/mandari && docker compose pull 2>/dev/null || true"

    info "Services starten..."
    ssh_cmd "$host" "cd /opt/mandari && docker compose up -d"

    info "Warte auf Services (60 Sekunden)..."
    sleep 60

    info "Service Status:"
    ssh_cmd "$host" "cd /opt/mandari && docker compose ps"

    log "$name Services gestartet!"
}

# =============================================================================
# HEALTH CHECK
# =============================================================================

check_health() {
    local host=$1
    local name=$2

    info "Health Check für $name..."

    local health=$(ssh_cmd "$host" "curl -sf http://localhost/health 2>/dev/null || echo 'FAIL'")

    if [[ "$health" == "OK" ]]; then
        log "$name Health Check: OK"
        return 0
    else
        warn "$name Health Check: FAIL (Services starten noch...)"
        return 1
    fi
}

# =============================================================================
# GITHUB SECRETS AUSGEBEN
# =============================================================================

print_github_instructions() {
    section "GitHub Konfiguration"

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  GITHUB SECRETS - Kopiere diese Werte in dein Repository     ║${NC}"
    echo -e "${CYAN}╠══════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║  Settings → Secrets and variables → Actions → Secrets        ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    echo -e "${YELLOW}Secret Name              │ Value${NC}"
    echo "─────────────────────────┼──────────────────────────────────────"
    echo -e "MASTER_IP                │ ${GREEN}${MASTER_PUBLIC_IP}${NC}"
    echo -e "SLAVE_IP                 │ ${GREEN}${SLAVE_PUBLIC_IP}${NC}"
    echo -e "POSTGRES_USER            │ ${GREEN}mandari${NC}"
    echo -e "POSTGRES_PASSWORD        │ ${GREEN}${POSTGRES_PASSWORD}${NC}"
    echo -e "POSTGRES_DB              │ ${GREEN}mandari${NC}"
    echo -e "SECRET_KEY               │ ${GREEN}${SECRET_KEY}${NC}"
    echo -e "ENCRYPTION_MASTER_KEY    │ ${GREEN}${ENCRYPTION_MASTER_KEY}${NC}"
    echo -e "MEILISEARCH_KEY          │ ${GREEN}${MEILISEARCH_KEY}${NC}"
    echo -e "SITE_URL                 │ ${GREEN}https://${DOMAIN}${NC}"
    echo -e "SSH_PRIVATE_KEY          │ ${YELLOW}(Inhalt von ~/.ssh/id_ed25519)${NC}"
    echo ""

    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  GITHUB VARIABLE - Aktiviert das Deployment                  ║${NC}"
    echo -e "${CYAN}╠══════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║  Settings → Secrets and variables → Actions → Variables      ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${YELLOW}Variable Name            │ Value${NC}"
    echo "─────────────────────────┼──────────────────────────────────────"
    echo -e "DEPLOYMENT_ENABLED       │ ${GREEN}true${NC}"
    echo ""

    # SSH Key Hinweis
    echo -e "${YELLOW}[!] SSH_PRIVATE_KEY:${NC}"
    echo "    Führe aus: cat ~/.ssh/id_ed25519"
    echo "    Kopiere den GESAMTEN Inhalt (inkl. BEGIN/END Zeilen)"
    echo ""
}

# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

main() {
    clear
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                                                              ║${NC}"
    echo -e "${BLUE}║              MANDARI PRODUCTION SETUP                        ║${NC}"
    echo -e "${BLUE}║                                                              ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    echo -e "${CYAN}Server Konfiguration:${NC}"
    echo "  Load Balancer: ${LB_PUBLIC_IP} (Private: ${LB_PRIVATE_IP})"
    echo "  Master:        ${MASTER_PUBLIC_IP} (Private: ${MASTER_PRIVATE_IP})"
    echo "  Slave:         ${SLAVE_PUBLIC_IP} (Private: ${SLAVE_PRIVATE_IP})"
    echo "  Domain:        ${DOMAIN}"
    echo ""

    read -p "Fortfahren? (j/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Jj]$ ]]; then
        exit 0
    fi

    # Secrets laden oder generieren
    generate_secrets

    # SSH Verbindungen testen
    section "SSH Verbindungen testen"
    test_ssh "$MASTER_PUBLIC_IP" "Master"
    test_ssh "$SLAVE_PUBLIC_IP" "Slave"

    # Basis-Setup
    setup_server_base "$MASTER_PUBLIC_IP" "Master"
    setup_server_base "$SLAVE_PUBLIC_IP" "Slave"

    # Docker Compose erstellen
    create_master_compose
    create_slave_compose

    # Konfigurationsdateien
    create_configs "$MASTER_PUBLIC_IP" "Master"
    create_configs "$SLAVE_PUBLIC_IP" "Slave"

    # Services starten
    start_services "$MASTER_PUBLIC_IP" "Master"
    start_services "$SLAVE_PUBLIC_IP" "Slave"

    # Health Checks
    section "Health Checks"
    check_health "$MASTER_PUBLIC_IP" "Master" || true
    check_health "$SLAVE_PUBLIC_IP" "Slave" || true

    # GitHub Anweisungen
    print_github_instructions

    section "Setup abgeschlossen!"

    echo -e "${GREEN}Nächste Schritte:${NC}"
    echo ""
    echo "  1. DNS prüfen: ${DOMAIN} → ${LB_PUBLIC_IP}"
    echo ""
    echo "  2. GitHub Secrets eintragen (siehe oben)"
    echo ""
    echo "  3. GitHub Variable setzen: DEPLOYMENT_ENABLED = true"
    echo ""
    echo "  4. Deployment auslösen:"
    echo "     git push origin main"
    echo "     ODER: Actions → Deploy Mandari → Run workflow"
    echo ""
    echo -e "${CYAN}Nützliche Befehle:${NC}"
    echo ""
    echo "  # Status prüfen"
    echo "  ssh root@${MASTER_PUBLIC_IP} 'cd /opt/mandari && docker compose ps'"
    echo ""
    echo "  # Logs anzeigen"
    echo "  ssh root@${MASTER_PUBLIC_IP} 'cd /opt/mandari && docker compose logs -f api'"
    echo ""
    echo "  # Secrets-Datei"
    echo "  cat ${SECRETS_FILE}"
    echo ""
}

# Ausführen
main "$@"
