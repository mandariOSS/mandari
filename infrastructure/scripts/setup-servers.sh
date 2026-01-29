#!/bin/bash
# =============================================================================
# Mandari Server Setup Script
# =============================================================================
# Dieses Script konfiguriert die Hetzner Server für Mandari.
#
# Voraussetzungen:
# - SSH-Zugang zu beiden Servern (als root)
# - Server haben Ubuntu 24.04
#
# Verwendung:
#   ./setup-servers.sh
#
# =============================================================================

set -euo pipefail

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section() { echo -e "\n${BLUE}═══════════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}\n"; }

# =============================================================================
# KONFIGURATION - HIER ANPASSEN!
# =============================================================================

# Server IPs (ANPASSEN!)
MASTER_IP="${MASTER_IP:-}"
SLAVE_IP="${SLAVE_IP:-}"
MASTER_PRIVATE_IP="${MASTER_PRIVATE_IP:-10.0.1.10}"
SLAVE_PRIVATE_IP="${SLAVE_PRIVATE_IP:-10.0.1.11}"

# Domain
DOMAIN="${DOMAIN:-mandari.de}"

# Passwörter (werden generiert wenn leer)
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
REPLICATION_PASSWORD="${REPLICATION_PASSWORD:-}"
SECRET_KEY="${SECRET_KEY:-}"
ENCRYPTION_MASTER_KEY="${ENCRYPTION_MASTER_KEY:-}"
MEILISEARCH_KEY="${MEILISEARCH_KEY:-}"

# SSH
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-~/.ssh/id_ed25519}"

# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

generate_password() {
    openssl rand -base64 32 | tr -d '/+=' | cut -c1-32
}

generate_secret_key() {
    openssl rand -base64 64 | tr -d '/+=' | cut -c1-64
}

ssh_cmd() {
    local host=$1
    shift
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${SSH_USER}@${host}" "$@"
}

scp_file() {
    local src=$1
    local host=$2
    local dest=$3
    scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$src" "${SSH_USER}@${host}:${dest}"
}

# =============================================================================
# VALIDIERUNG
# =============================================================================

validate_config() {
    section "Konfiguration prüfen"

    if [[ -z "$MASTER_IP" ]]; then
        read -p "Master Server IP: " MASTER_IP
    fi

    if [[ -z "$SLAVE_IP" ]]; then
        read -p "Slave Server IP: " SLAVE_IP
    fi

    if [[ -z "$MASTER_IP" ]] || [[ -z "$SLAVE_IP" ]]; then
        error "Server IPs müssen angegeben werden!"
    fi

    # Generiere Passwörter wenn nicht gesetzt
    [[ -z "$POSTGRES_PASSWORD" ]] && POSTGRES_PASSWORD=$(generate_password)
    [[ -z "$REPLICATION_PASSWORD" ]] && REPLICATION_PASSWORD=$(generate_password)
    [[ -z "$SECRET_KEY" ]] && SECRET_KEY=$(generate_secret_key)
    [[ -z "$ENCRYPTION_MASTER_KEY" ]] && ENCRYPTION_MASTER_KEY=$(openssl rand -base64 32)
    [[ -z "$MEILISEARCH_KEY" ]] && MEILISEARCH_KEY=$(generate_password)

    log "Master IP: $MASTER_IP"
    log "Slave IP: $SLAVE_IP"
    log "Domain: $DOMAIN"

    # SSH Verbindung testen
    log "Teste SSH-Verbindung zu Master..."
    if ! ssh_cmd "$MASTER_IP" "echo 'OK'" > /dev/null 2>&1; then
        error "Keine SSH-Verbindung zu Master ($MASTER_IP)"
    fi

    log "Teste SSH-Verbindung zu Slave..."
    if ! ssh_cmd "$SLAVE_IP" "echo 'OK'" > /dev/null 2>&1; then
        error "Keine SSH-Verbindung zu Slave ($SLAVE_IP)"
    fi

    log "SSH-Verbindungen OK!"
}

# =============================================================================
# SERVER BASIS-SETUP
# =============================================================================

setup_base() {
    local host=$1
    local role=$2

    section "Basis-Setup: $role ($host)"

    log "System aktualisieren..."
    ssh_cmd "$host" "apt-get update && apt-get upgrade -y"

    log "Pakete installieren..."
    ssh_cmd "$host" "apt-get install -y \
        curl \
        gnupg \
        lsb-release \
        ca-certificates \
        ufw \
        fail2ban \
        htop \
        vim \
        git \
        jq"

    log "Docker installieren..."
    ssh_cmd "$host" "
        if ! command -v docker &> /dev/null; then
            curl -fsSL https://get.docker.com | sh
            systemctl enable docker
            systemctl start docker
        fi
    "

    log "Docker Compose installieren..."
    ssh_cmd "$host" "
        if ! command -v docker-compose &> /dev/null; then
            curl -L \"https://github.com/docker/compose/releases/latest/download/docker-compose-\$(uname -s)-\$(uname -m)\" -o /usr/local/bin/docker-compose
            chmod +x /usr/local/bin/docker-compose
        fi
    "

    log "Deploy User erstellen..."
    ssh_cmd "$host" "
        if ! id deploy &> /dev/null; then
            useradd --create-home --shell /bin/bash deploy
            usermod -aG docker deploy
        fi
    "

    log "App-Verzeichnisse erstellen..."
    ssh_cmd "$host" "
        mkdir -p /opt/mandari/{config,data}
        mkdir -p /opt/mandari/data/{postgres,redis,meilisearch,media}
        chown -R deploy:deploy /opt/mandari
    "

    log "Firewall konfigurieren..."
    ssh_cmd "$host" "
        ufw --force reset
        ufw default deny incoming
        ufw default allow outgoing
        ufw allow ssh
        ufw allow http
        ufw allow https
        ufw allow from 10.0.0.0/16  # Private Network
        ufw --force enable
    "

    log "Basis-Setup für $role abgeschlossen!"
}

# =============================================================================
# DOCKER COMPOSE ERSTELLEN
# =============================================================================

create_docker_compose() {
    local host=$1
    local role=$2  # master oder slave

    section "Docker Compose erstellen: $role"

    local compose_file="/tmp/docker-compose-${role}.yml"

    if [[ "$role" == "master" ]]; then
        cat > "$compose_file" << 'MASTEREOF'
version: "3.9"

services:
  # === REVERSE PROXY ===
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
      - api

  # === DATABASE (PRIMARY) ===
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
      - "wal_level=replica"
      - "-c"
      - "max_wal_senders=3"
      - "-c"
      - "max_replication_slots=3"
      - "-c"
      - "hot_standby=on"
      - "-c"
      - "listen_addresses=*"
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
      - ./config/pg_hba.conf:/var/lib/postgresql/data/pg_hba.conf:ro
    networks:
      - mandari
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mandari"]
      interval: 10s
      timeout: 5s
      retries: 5

  # === CACHE ===
  redis:
    image: redis:7-alpine
    container_name: mandari-redis
    restart: unless-stopped
    command: >
      redis-server
      --appendonly yes
      --maxmemory 512mb
      --maxmemory-policy allkeys-lru
    volumes:
      - ./data/redis:/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  # === SEARCH ===
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
      test: ["CMD", "curl", "-f", "http://localhost:7700/health"]
      interval: 30s
      timeout: 10s
      retries: 5

  # === API (Django) ===
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
      ALLOWED_HOSTS: ${DOMAIN},localhost
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
      test: ["CMD", "curl", "-f", "http://localhost:8000/health/"]
      interval: 30s
      timeout: 10s
      retries: 5

  # === INGESTOR (nur auf Master!) ===
  ingestor:
    image: ghcr.io/mandarioss/mandari-ingestor:${IMAGE_TAG:-latest}
    container_name: mandari-ingestor
    restart: unless-stopped
    command: ["python", "-m", "src.main", "daemon", "--interval", "15"]
    environment:
      DATABASE_URL: postgresql+asyncpg://mandari:${POSTGRES_PASSWORD}@postgres:5432/mandari
      REDIS_URL: redis://redis:6379
      MEILISEARCH_URL: http://meilisearch:7700
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
    networks:
      - mandari
    depends_on:
      postgres:
        condition: service_healthy

networks:
  mandari:
    driver: bridge
MASTEREOF
    else
        # Slave config (kein Ingestor, PostgreSQL als Replica)
        cat > "$compose_file" << 'SLAVEEOF'
version: "3.9"

services:
  # === REVERSE PROXY ===
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
      - api

  # === DATABASE (REPLICA) ===
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
      - "hot_standby=on"
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    networks:
      - mandari
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mandari"]
      interval: 10s
      timeout: 5s
      retries: 5

  # === CACHE (REPLICA) ===
  redis:
    image: redis:7-alpine
    container_name: mandari-redis
    restart: unless-stopped
    command: >
      redis-server
      --appendonly yes
      --maxmemory 512mb
      --maxmemory-policy allkeys-lru
      --replicaof ${MASTER_PRIVATE_IP} 6379
    volumes:
      - ./data/redis:/data
    networks:
      - mandari

  # === SEARCH ===
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

  # === API (Django) ===
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
      ALLOWED_HOSTS: ${DOMAIN},localhost
    volumes:
      - ./data/media:/app/media
    networks:
      - mandari
    depends_on:
      postgres:
        condition: service_healthy

  # KEIN INGESTOR auf Slave!

networks:
  mandari:
    driver: bridge
SLAVEEOF
    fi

    # Datei auf Server kopieren
    scp_file "$compose_file" "$host" "/opt/mandari/docker-compose.yml"
    rm "$compose_file"

    log "Docker Compose für $role erstellt!"
}

# =============================================================================
# KONFIGURATIONSDATEIEN ERSTELLEN
# =============================================================================

create_config_files() {
    local host=$1
    local role=$2

    section "Konfigurationsdateien erstellen: $role"

    # Caddyfile
    local caddyfile="/tmp/Caddyfile-${role}"
    cat > "$caddyfile" << EOF
{
    email admin@${DOMAIN}
}

${DOMAIN} {
    reverse_proxy api:8000

    header {
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
    }

    handle /static/* {
        root * /srv
        file_server
    }

    handle /media/* {
        root * /srv
        file_server
    }

    log {
        output file /data/access.log
    }
}

# Health check endpoint (für Load Balancer)
:80 {
    respond /health "OK" 200
}
EOF

    scp_file "$caddyfile" "$host" "/opt/mandari/config/Caddyfile"
    rm "$caddyfile"

    # pg_hba.conf (nur Master braucht Replikation)
    if [[ "$role" == "master" ]]; then
        local pg_hba="/tmp/pg_hba.conf"
        cat > "$pg_hba" << EOF
# TYPE  DATABASE        USER            ADDRESS                 METHOD
local   all             all                                     trust
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
host    all             all             10.0.0.0/16             scram-sha-256
host    replication     replicator      10.0.0.0/16             scram-sha-256
EOF
        scp_file "$pg_hba" "$host" "/opt/mandari/config/pg_hba.conf"
        rm "$pg_hba"
    fi

    # .env Datei
    local envfile="/tmp/env-${role}"
    cat > "$envfile" << EOF
# Mandari Environment - ${role^^}
# Generated: $(date)

DOMAIN=${DOMAIN}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
REPLICATION_PASSWORD=${REPLICATION_PASSWORD}
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_MASTER_KEY=${ENCRYPTION_MASTER_KEY}
MEILISEARCH_KEY=${MEILISEARCH_KEY}
MASTER_PRIVATE_IP=${MASTER_PRIVATE_IP}
IMAGE_TAG=latest
EOF

    scp_file "$envfile" "$host" "/opt/mandari/.env"
    rm "$envfile"

    # Berechtigungen setzen
    ssh_cmd "$host" "chmod 600 /opt/mandari/.env && chown deploy:deploy /opt/mandari/.env"

    log "Konfigurationsdateien für $role erstellt!"
}

# =============================================================================
# POSTGRESQL REPLIKATION EINRICHTEN
# =============================================================================

setup_postgres_replication() {
    section "PostgreSQL Replikation einrichten"

    log "Warte auf PostgreSQL Start auf Master..."
    ssh_cmd "$MASTER_IP" "cd /opt/mandari && docker-compose up -d postgres"
    sleep 10

    log "Replikations-User erstellen..."
    ssh_cmd "$MASTER_IP" "docker exec mandari-postgres psql -U mandari -c \"
        DO \\\$\\\$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'replicator') THEN
                CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '${REPLICATION_PASSWORD}';
            END IF;
        END
        \\\$\\\$;
    \""

    log "Replikation auf Slave vorbereiten..."
    # Auf Slave: Daten vom Master holen
    ssh_cmd "$SLAVE_IP" "
        cd /opt/mandari
        docker-compose down postgres 2>/dev/null || true
        rm -rf /opt/mandari/data/postgres/*

        # pg_basebackup vom Master
        docker run --rm \
            -v /opt/mandari/data/postgres:/var/lib/postgresql/data \
            -e PGPASSWORD='${REPLICATION_PASSWORD}' \
            postgres:16-alpine \
            pg_basebackup -h ${MASTER_PRIVATE_IP} -D /var/lib/postgresql/data -U replicator -Fp -Xs -P -R

        # standby.signal erstellen
        touch /opt/mandari/data/postgres/standby.signal
        chown -R 999:999 /opt/mandari/data/postgres
    "

    log "PostgreSQL Replikation eingerichtet!"
}

# =============================================================================
# SERVICES STARTEN
# =============================================================================

start_services() {
    local host=$1
    local role=$2

    section "Services starten: $role"

    ssh_cmd "$host" "cd /opt/mandari && docker-compose pull"
    ssh_cmd "$host" "cd /opt/mandari && docker-compose up -d"

    log "Warte auf Services..."
    sleep 15

    ssh_cmd "$host" "cd /opt/mandari && docker-compose ps"

    log "Services auf $role gestartet!"
}

# =============================================================================
# GITHUB SECRETS AUSGEBEN
# =============================================================================

print_github_secrets() {
    section "GitHub Secrets"

    echo ""
    echo "Füge diese Secrets in GitHub Repository Settings hinzu:"
    echo "Settings → Secrets and variables → Actions → New repository secret"
    echo ""
    echo "═══════════════════════════════════════════════════════════"
    printf "%-25s %s\n" "MASTER_IP:" "$MASTER_IP"
    printf "%-25s %s\n" "SLAVE_IP:" "$SLAVE_IP"
    printf "%-25s %s\n" "SSH_PRIVATE_KEY:" "(Inhalt von $SSH_KEY)"
    printf "%-25s %s\n" "POSTGRES_USER:" "mandari"
    printf "%-25s %s\n" "POSTGRES_PASSWORD:" "$POSTGRES_PASSWORD"
    printf "%-25s %s\n" "POSTGRES_DB:" "mandari"
    printf "%-25s %s\n" "REPLICATION_PASSWORD:" "$REPLICATION_PASSWORD"
    printf "%-25s %s\n" "SECRET_KEY:" "$SECRET_KEY"
    printf "%-25s %s\n" "ENCRYPTION_MASTER_KEY:" "$ENCRYPTION_MASTER_KEY"
    printf "%-25s %s\n" "MEILISEARCH_KEY:" "$MEILISEARCH_KEY"
    printf "%-25s %s\n" "SITE_URL:" "https://${DOMAIN}"
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo "Außerdem als Repository Variable setzen:"
    echo "Settings → Secrets and variables → Actions → Variables → New repository variable"
    echo ""
    printf "%-25s %s\n" "DEPLOYMENT_ENABLED:" "true"
    echo ""

    # Speichere Secrets in Datei
    local secrets_file="./secrets-$(date +%Y%m%d-%H%M%S).txt"
    cat > "$secrets_file" << EOF
# Mandari Secrets - Generated $(date)
# ACHTUNG: Diese Datei sicher aufbewahren und nach Verwendung löschen!

MASTER_IP=${MASTER_IP}
SLAVE_IP=${SLAVE_IP}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
REPLICATION_PASSWORD=${REPLICATION_PASSWORD}
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_MASTER_KEY=${ENCRYPTION_MASTER_KEY}
MEILISEARCH_KEY=${MEILISEARCH_KEY}
DOMAIN=${DOMAIN}
EOF
    chmod 600 "$secrets_file"
    warn "Secrets gespeichert in: $secrets_file"
    warn "Diese Datei nach dem Einrichten der GitHub Secrets LÖSCHEN!"
}

# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

main() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║           MANDARI SERVER SETUP SCRIPT                     ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    validate_config

    # Basis-Setup auf beiden Servern
    setup_base "$MASTER_IP" "Master"
    setup_base "$SLAVE_IP" "Slave"

    # Docker Compose erstellen
    create_docker_compose "$MASTER_IP" "master"
    create_docker_compose "$SLAVE_IP" "slave"

    # Konfigurationsdateien erstellen
    create_config_files "$MASTER_IP" "master"
    create_config_files "$SLAVE_IP" "slave"

    # Master starten (ohne Replikation zunächst)
    start_services "$MASTER_IP" "master"

    # Replikation einrichten
    # setup_postgres_replication  # Optional - erstmal ohne

    # Slave starten
    start_services "$SLAVE_IP" "slave"

    # GitHub Secrets ausgeben
    print_github_secrets

    section "Setup abgeschlossen!"

    echo ""
    log "Nächste Schritte:"
    echo "  1. GitHub Secrets eintragen (siehe oben)"
    echo "  2. Repository Variable DEPLOYMENT_ENABLED=true setzen"
    echo "  3. git push auslösen oder Workflow manuell starten"
    echo ""
    log "Server Status prüfen:"
    echo "  Master: ssh ${SSH_USER}@${MASTER_IP} 'cd /opt/mandari && docker-compose ps'"
    echo "  Slave:  ssh ${SSH_USER}@${SLAVE_IP} 'cd /opt/mandari && docker-compose ps'"
    echo ""
    log "Logs anzeigen:"
    echo "  ssh ${SSH_USER}@${MASTER_IP} 'cd /opt/mandari && docker-compose logs -f'"
    echo ""
}

# Ausführen
main "$@"
