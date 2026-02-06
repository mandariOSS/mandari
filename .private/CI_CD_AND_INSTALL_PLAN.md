# CI/CD Pipeline & Install-Skript Plan

**Erstellt**: 2026-02-05
**Status**: Planung
**Ziel**: Öffentliche Build-Pipeline + einfaches Install-Skript

---

## Aktuelle Situation

### Was existiert
- `pr-check.yml` - Lint + Test + Docker Build Test bei PRs
- Docker Images werden im **privaten** infra-Repo gebaut
- Makefile mit vielen Targets
- Umfangreiche Ansible/Terraform Infrastruktur

### Was fehlt
- **Automatische Docker Image Builds** bei Push auf main/Tags
- **Öffentliche Registry** (ghcr.io) mit versionierten Images
- **Einfaches Install-Skript** für Selbst-Hosting
- **Release-Workflow** mit Changelog

---

## Teil 1: GitHub Actions Pipeline

### 1.1 Neue Workflow-Datei: `build-and-publish.yml`

**Trigger**:
```yaml
on:
  push:
    branches: [main]
    tags: ['v*']
  workflow_dispatch:  # Manuell
```

**Jobs**:

```
┌─────────────────────────────────────────────────────────────┐
│                    build-and-publish.yml                     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────┐    ┌─────────┐    ┌─────────────────────────┐ │
│  │  Lint   │───→│  Test   │───→│  Build & Push Images    │ │
│  └─────────┘    └─────────┘    └───────────┬─────────────┘ │
│                                            │               │
│                                            ▼               │
│                               ┌─────────────────────────┐  │
│                               │  Create GitHub Release  │  │
│                               │  (nur bei Tags)         │  │
│                               └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Workflow-Definition

```yaml
# .github/workflows/build-and-publish.yml
name: Build and Publish

on:
  push:
    branches: [main]
    tags: ['v*']
  workflow_dispatch:
    inputs:
      skip_tests:
        description: 'Skip tests (for hotfixes)'
        type: boolean
        default: false

env:
  REGISTRY: ghcr.io
  MANDARI_IMAGE: ghcr.io/mandarioss/mandari
  INGESTOR_IMAGE: ghcr.io/mandarioss/ingestor

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install linters
        run: pip install ruff black isort
      - name: Run linters
        run: |
          cd mandari
          ruff check .
          black --check .
          isort --check-only .

  test:
    needs: lint
    if: ${{ !inputs.skip_tests }}
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: test_mandari
          POSTGRES_USER: mandari
          POSTGRES_PASSWORD: test
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432
      redis:
        image: redis:7-alpine
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install system dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y libpq-dev libcairo2-dev libpango1.0-dev \
            libffi-dev poppler-utils tesseract-ocr tesseract-ocr-deu

      - name: Install Python dependencies
        run: |
          cd mandari
          pip install -e ".[dev]"

      - name: Run Django tests
        env:
          DATABASE_URL: postgres://mandari:test@localhost:5432/test_mandari
          REDIS_URL: redis://localhost:6379
          SECRET_KEY: test-secret-key
          ENCRYPTION_MASTER_KEY: dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcw==
        run: |
          cd mandari
          python manage.py migrate --noinput
          pytest -v --tb=short

      - name: Run Ingestor tests
        run: |
          cd ingestor
          pip install -e ".[dev]"
          pytest -v

  build-and-push:
    needs: [lint, test]
    if: always() && needs.lint.result == 'success' && (needs.test.result == 'success' || needs.test.result == 'skipped')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Generate image tags
        id: tags
        run: |
          SHA_SHORT=$(echo ${{ github.sha }} | cut -c1-7)

          if [[ "${{ github.ref }}" == refs/tags/v* ]]; then
            VERSION=${GITHUB_REF#refs/tags/v}
            echo "mandari_tags=${{ env.MANDARI_IMAGE }}:${VERSION},${{ env.MANDARI_IMAGE }}:latest,${{ env.MANDARI_IMAGE }}:sha-${SHA_SHORT}" >> $GITHUB_OUTPUT
            echo "ingestor_tags=${{ env.INGESTOR_IMAGE }}:${VERSION},${{ env.INGESTOR_IMAGE }}:latest,${{ env.INGESTOR_IMAGE }}:sha-${SHA_SHORT}" >> $GITHUB_OUTPUT
            echo "version=${VERSION}" >> $GITHUB_OUTPUT
          else
            echo "mandari_tags=${{ env.MANDARI_IMAGE }}:main,${{ env.MANDARI_IMAGE }}:sha-${SHA_SHORT}" >> $GITHUB_OUTPUT
            echo "ingestor_tags=${{ env.INGESTOR_IMAGE }}:main,${{ env.INGESTOR_IMAGE }}:sha-${SHA_SHORT}" >> $GITHUB_OUTPUT
            echo "version=main" >> $GITHUB_OUTPUT
          fi

      - name: Build and push Mandari API
        uses: docker/build-push-action@v5
        with:
          context: ./mandari
          file: ./mandari/Dockerfile
          push: true
          tags: ${{ steps.tags.outputs.mandari_tags }}
          platforms: linux/amd64,linux/arm64
          cache-from: type=gha,scope=mandari
          cache-to: type=gha,mode=max,scope=mandari
          labels: |
            org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}
            org.opencontainers.image.revision=${{ github.sha }}
            org.opencontainers.image.version=${{ steps.tags.outputs.version }}

      - name: Build and push Ingestor
        uses: docker/build-push-action@v5
        with:
          context: ./ingestor
          file: ./ingestor/Dockerfile
          push: true
          tags: ${{ steps.tags.outputs.ingestor_tags }}
          platforms: linux/amd64,linux/arm64
          cache-from: type=gha,scope=ingestor
          cache-to: type=gha,mode=max,scope=ingestor
          labels: |
            org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}
            org.opencontainers.image.revision=${{ github.sha }}
            org.opencontainers.image.version=${{ steps.tags.outputs.version }}

    outputs:
      version: ${{ steps.tags.outputs.version }}

  release:
    needs: build-and-push
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Für Changelog

      - name: Generate changelog
        id: changelog
        run: |
          # Letztes Tag finden
          PREV_TAG=$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "")

          if [ -n "$PREV_TAG" ]; then
            CHANGES=$(git log ${PREV_TAG}..HEAD --pretty=format:"- %s" --no-merges)
          else
            CHANGES=$(git log --pretty=format:"- %s" --no-merges -20)
          fi

          echo "changes<<EOF" >> $GITHUB_OUTPUT
          echo "$CHANGES" >> $GITHUB_OUTPUT
          echo "EOF" >> $GITHUB_OUTPUT

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v1
        with:
          name: Mandari ${{ needs.build-and-push.outputs.version }}
          body: |
            ## Docker Images

            ```bash
            docker pull ghcr.io/mandarioss/mandari:${{ needs.build-and-push.outputs.version }}
            docker pull ghcr.io/mandarioss/ingestor:${{ needs.build-and-push.outputs.version }}
            ```

            ## Quick Install

            ```bash
            curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/install.sh | bash
            ```

            ## Changes

            ${{ steps.changelog.outputs.changes }}

          draft: false
          prerelease: ${{ contains(github.ref, '-rc') || contains(github.ref, '-beta') }}
```

### 1.3 Image-Tagging-Strategie

| Event | Tags |
|-------|------|
| Push auf `main` | `main`, `sha-abc1234` |
| Tag `v1.2.3` | `1.2.3`, `latest`, `sha-abc1234` |
| Tag `v1.2.3-rc1` | `1.2.3-rc1`, `sha-abc1234` (kein `latest`) |

---

## Teil 2: Install-Skript

### 2.1 Design-Prinzipien

- **Ein Befehl**: `curl -fsSL .../install.sh | bash`
- **Interaktiv**: Fragt nach Domain, Passwörtern
- **Idempotent**: Kann mehrfach ausgeführt werden
- **Kein Branding**: Nicht "Community Edition", einfach "Mandari"

### 2.2 Skript-Struktur

```bash
#!/usr/bin/env bash
# install.sh - Mandari Installation Script
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/install.sh | bash
#
# Or download and run:
#   wget https://raw.githubusercontent.com/mandarioss/mandari/main/install.sh
#   chmod +x install.sh
#   ./install.sh

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

MANDARI_VERSION="${MANDARI_VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/opt/mandari}"
DATA_DIR="${DATA_DIR:-/var/lib/mandari}"

MANDARI_IMAGE="ghcr.io/mandarioss/mandari:${MANDARI_VERSION}"
INGESTOR_IMAGE="ghcr.io/mandarioss/ingestor:${MANDARI_VERSION}"

# ============================================================================
# Helper Functions
# ============================================================================

info() { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
error() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }
success() { echo -e "\033[1;32m[OK]\033[0m $*"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
    fi
}

check_system() {
    info "Checking system requirements..."

    # OS Check
    if [[ ! -f /etc/os-release ]]; then
        error "Unsupported operating system"
    fi
    source /etc/os-release

    case "$ID" in
        ubuntu|debian)
            success "Detected $PRETTY_NAME"
            ;;
        *)
            warn "Untested OS: $PRETTY_NAME (proceeding anyway)"
            ;;
    esac

    # Memory Check
    TOTAL_MEM=$(free -m | awk '/^Mem:/{print $2}')
    if [[ $TOTAL_MEM -lt 2048 ]]; then
        warn "Less than 2GB RAM detected ($TOTAL_MEM MB). Mandari may run slowly."
    fi

    # Disk Space Check
    AVAILABLE_DISK=$(df -BG "$INSTALL_DIR" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G' || echo "0")
    if [[ ${AVAILABLE_DISK:-0} -lt 10 ]]; then
        warn "Less than 10GB disk space available."
    fi
}

install_docker() {
    if command -v docker &>/dev/null; then
        success "Docker already installed: $(docker --version)"
        return
    fi

    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    success "Docker installed"
}

install_docker_compose() {
    if docker compose version &>/dev/null; then
        success "Docker Compose already available"
        return
    fi

    info "Docker Compose plugin not found, installing..."
    apt-get update
    apt-get install -y docker-compose-plugin
    success "Docker Compose installed"
}

generate_secrets() {
    info "Generating secrets..."

    # Django Secret Key
    SECRET_KEY=$(openssl rand -base64 48 | tr -dc 'a-zA-Z0-9' | head -c 64)

    # Encryption Key (32 bytes = 256 bit, base64 encoded)
    ENCRYPTION_KEY=$(openssl rand -base64 32)

    # Database Password
    POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 32)

    # Meilisearch Key
    MEILISEARCH_KEY=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 32)

    success "Secrets generated"
}

prompt_configuration() {
    echo ""
    echo "============================================"
    echo "  Mandari Configuration"
    echo "============================================"
    echo ""

    # Domain
    read -rp "Domain (e.g., mandari.example.com): " DOMAIN
    if [[ -z "$DOMAIN" ]]; then
        error "Domain is required"
    fi

    # Email for Let's Encrypt
    read -rp "Email for SSL certificates: " ACME_EMAIL
    if [[ -z "$ACME_EMAIL" ]]; then
        warn "No email provided. SSL certificates may fail."
        ACME_EMAIL="admin@${DOMAIN}"
    fi

    # OParl Source (optional)
    echo ""
    echo "Optional: Configure an OParl data source"
    echo "Example: https://sdnetrim.kdvz-frechen.de/rim4550/webservice/oparl/v1/system"
    read -rp "OParl API URL (leave empty to skip): " OPARL_SOURCE

    echo ""
    success "Configuration complete"
}

create_directories() {
    info "Creating directories..."

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$DATA_DIR/postgres"
    mkdir -p "$DATA_DIR/redis"
    mkdir -p "$DATA_DIR/meilisearch"
    mkdir -p "$DATA_DIR/caddy"

    success "Directories created"
}

download_files() {
    info "Downloading configuration files..."

    cd "$INSTALL_DIR"

    # Docker Compose
    curl -fsSL "https://raw.githubusercontent.com/mandarioss/mandari/main/docker-compose.yml" \
        -o docker-compose.yml

    # Caddyfile
    curl -fsSL "https://raw.githubusercontent.com/mandarioss/mandari/main/Caddyfile" \
        -o Caddyfile

    success "Files downloaded"
}

create_env_file() {
    info "Creating .env file..."

    cat > "$INSTALL_DIR/.env" <<EOF
# Mandari Configuration
# Generated: $(date -Iseconds)

# =============================================================================
# Core Settings
# =============================================================================

DOMAIN=${DOMAIN}
SITE_URL=https://${DOMAIN}
ALLOWED_HOSTS=${DOMAIN}

# =============================================================================
# Security (DO NOT SHARE!)
# =============================================================================

SECRET_KEY=${SECRET_KEY}
ENCRYPTION_MASTER_KEY=${ENCRYPTION_KEY}

# =============================================================================
# Database
# =============================================================================

POSTGRES_DB=mandari
POSTGRES_USER=mandari
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DATABASE_URL=postgresql://mandari:${POSTGRES_PASSWORD}@postgres:5432/mandari

# =============================================================================
# Redis
# =============================================================================

REDIS_URL=redis://redis:6379

# =============================================================================
# Search
# =============================================================================

MEILISEARCH_URL=http://meilisearch:7700
MEILISEARCH_KEY=${MEILISEARCH_KEY}

# =============================================================================
# SSL / Caddy
# =============================================================================

ACME_EMAIL=${ACME_EMAIL}

# =============================================================================
# Images
# =============================================================================

IMAGE_TAG=${MANDARI_VERSION}

# =============================================================================
# OParl Sync (Optional)
# =============================================================================

OPARL_DEFAULT_SOURCE=${OPARL_SOURCE:-}
SYNC_INTERVAL_MINUTES=15
SYNC_FULL_HOUR=3
EOF

    chmod 600 "$INSTALL_DIR/.env"
    success ".env file created"
}

update_caddyfile() {
    info "Configuring Caddyfile..."

    cat > "$INSTALL_DIR/Caddyfile" <<EOF
{
    email ${ACME_EMAIL}
}

${DOMAIN} {
    reverse_proxy api:8000

    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }

    handle_path /static/* {
        root * /srv
        file_server
    }

    handle_path /media/* {
        root * /srv
        file_server
    }

    log {
        output file /var/log/caddy/access.log
        format json
    }
}
EOF

    success "Caddyfile configured"
}

pull_images() {
    info "Pulling Docker images (this may take a few minutes)..."

    cd "$INSTALL_DIR"
    docker compose pull

    success "Images pulled"
}

start_services() {
    info "Starting services..."

    cd "$INSTALL_DIR"
    docker compose up -d

    success "Services started"
}

wait_for_healthy() {
    info "Waiting for services to be healthy..."

    local max_attempts=60
    local attempt=0

    while [[ $attempt -lt $max_attempts ]]; do
        if curl -sf "http://localhost/health" &>/dev/null; then
            success "Mandari is running!"
            return 0
        fi

        attempt=$((attempt + 1))
        echo -n "."
        sleep 2
    done

    warn "Health check timed out. Check logs with: docker compose -f $INSTALL_DIR/docker-compose.yml logs"
    return 1
}

init_oparl_source() {
    if [[ -z "${OPARL_SOURCE:-}" ]]; then
        return
    fi

    info "Initializing OParl source..."

    cd "$INSTALL_DIR"
    docker compose exec -T ingestor python -m src.main add-source "$OPARL_SOURCE"
    docker compose exec -T ingestor python -m src.main sync --source "$OPARL_SOURCE"

    success "OParl source initialized"
}

print_summary() {
    echo ""
    echo "============================================"
    echo "  Installation Complete!"
    echo "============================================"
    echo ""
    echo "  URL: https://${DOMAIN}"
    echo ""
    echo "  Installation directory: ${INSTALL_DIR}"
    echo "  Data directory: ${DATA_DIR}"
    echo ""
    echo "  Useful commands:"
    echo "    cd ${INSTALL_DIR}"
    echo "    docker compose logs -f        # View logs"
    echo "    docker compose restart        # Restart services"
    echo "    docker compose down           # Stop services"
    echo "    docker compose pull && docker compose up -d  # Update"
    echo ""
    echo "  Configuration: ${INSTALL_DIR}/.env"
    echo ""
    if [[ -n "${OPARL_SOURCE:-}" ]]; then
        echo "  OParl sync is running. First sync may take 10-30 minutes."
        echo ""
    fi
    echo "============================================"
}

# ============================================================================
# Main
# ============================================================================

main() {
    echo ""
    echo "============================================"
    echo "  Mandari Installation Script"
    echo "  Version: ${MANDARI_VERSION}"
    echo "============================================"
    echo ""

    check_root
    check_system
    install_docker
    install_docker_compose
    generate_secrets
    prompt_configuration
    create_directories
    download_files
    create_env_file
    update_caddyfile
    pull_images
    start_services
    wait_for_healthy
    init_oparl_source
    print_summary
}

main "$@"
```

### 2.3 Unattended Installation

Für automatisierte Setups:

```bash
# Alle Variablen vorher setzen
export DOMAIN="mandari.example.com"
export ACME_EMAIL="admin@example.com"
export OPARL_SOURCE="https://api.example.com/oparl"
export MANDARI_VERSION="1.0.0"

# Nicht-interaktiv ausführen
curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/install.sh | bash -s -- --unattended
```

---

## Teil 3: Zusätzliche Dateien

### 3.1 Öffentliche docker-compose.yml (vereinfacht)

```yaml
# docker-compose.yml (für install.sh)
# Minimale Single-Server Konfiguration

version: "3.8"

services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - api
    networks:
      - mandari

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-mandari}
      POSTGRES_USER: ${POSTGRES_USER:-mandari}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mandari"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - mandari

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - mandari

  meilisearch:
    image: getmeili/meilisearch:v1.6
    restart: unless-stopped
    environment:
      MEILI_MASTER_KEY: ${MEILISEARCH_KEY:?MEILISEARCH_KEY required}
      MEILI_NO_ANALYTICS: "true"
    volumes:
      - meilisearch_data:/meili_data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:7700/health"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - mandari

  api:
    image: ghcr.io/mandarioss/mandari:${IMAGE_TAG:-latest}
    restart: unless-stopped
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL:-redis://redis:6379}
      MEILISEARCH_URL: ${MEILISEARCH_URL:-http://meilisearch:7700}
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
      SECRET_KEY: ${SECRET_KEY:?SECRET_KEY required}
      ENCRYPTION_MASTER_KEY: ${ENCRYPTION_MASTER_KEY:?ENCRYPTION_MASTER_KEY required}
      ALLOWED_HOSTS: ${ALLOWED_HOSTS:-localhost}
      SITE_URL: ${SITE_URL:-http://localhost}
      DEBUG: "false"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      meilisearch:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks:
      - mandari

  ingestor:
    image: ghcr.io/mandarioss/ingestor:${IMAGE_TAG:-latest}
    restart: unless-stopped
    command: >
      python -m src.main daemon
      --interval ${SYNC_INTERVAL_MINUTES:-15}
      --full-sync-hour ${SYNC_FULL_HOUR:-3}
    environment:
      DATABASE_URL: ${DATABASE_URL}
      REDIS_URL: ${REDIS_URL:-redis://redis:6379}
      MEILISEARCH_URL: ${MEILISEARCH_URL:-http://meilisearch:7700}
      MEILISEARCH_KEY: ${MEILISEARCH_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - mandari

networks:
  mandari:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  meilisearch_data:
  caddy_data:
  caddy_config:
```

### 3.2 Update-Skript

```bash
#!/usr/bin/env bash
# update.sh - Update Mandari to latest version

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/mandari}"

cd "$INSTALL_DIR"

echo "Pulling latest images..."
docker compose pull

echo "Restarting services..."
docker compose up -d

echo "Waiting for health check..."
sleep 10

if curl -sf "http://localhost/health" &>/dev/null; then
    echo "Update complete!"
else
    echo "Warning: Health check failed. Check logs:"
    echo "  docker compose logs"
fi
```

### 3.3 Backup-Skript

```bash
#!/usr/bin/env bash
# backup.sh - Backup Mandari data

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/mandari}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mandari}"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

cd "$INSTALL_DIR"

echo "Creating database backup..."
docker compose exec -T postgres pg_dump -U mandari mandari | gzip > "$BACKUP_DIR/db_${DATE}.sql.gz"

echo "Backing up configuration..."
cp .env "$BACKUP_DIR/env_${DATE}.bak"

echo "Backup complete: $BACKUP_DIR"
ls -lh "$BACKUP_DIR"/*_${DATE}*
```

---

## Teil 4: Repository-Struktur

### 4.1 Neue Dateien im öffentlichen Repo

```
mandarioss/mandari/
├── .github/
│   └── workflows/
│       ├── pr-check.yml          # Existiert
│       └── build-and-publish.yml # NEU
├── install.sh                    # NEU
├── update.sh                     # NEU
├── backup.sh                     # NEU
├── docker-compose.yml            # Vereinfacht für Self-Hosting
├── Caddyfile.example             # NEU
├── .env.example                  # Existiert (erweitern)
├── INSTALL.md                    # NEU
├── mandari/
│   └── Dockerfile
├── ingestor/
│   └── Dockerfile
└── ...
```

### 4.2 INSTALL.md

```markdown
# Mandari Installation

## Quick Install (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/install.sh | sudo bash
```

## Requirements

- Linux server (Ubuntu 22.04+ recommended)
- 2GB+ RAM
- 10GB+ disk space
- Domain with DNS pointing to server
- Ports 80 and 443 open

## Manual Installation

1. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

2. Download files:
   ```bash
   mkdir -p /opt/mandari && cd /opt/mandari
   curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/docker-compose.yml -o docker-compose.yml
   curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/.env.example -o .env
   ```

3. Edit `.env` with your configuration

4. Start:
   ```bash
   docker compose up -d
   ```

## Updating

```bash
cd /opt/mandari
docker compose pull
docker compose up -d
```

## Backup

```bash
curl -fsSL https://raw.githubusercontent.com/mandarioss/mandari/main/backup.sh | sudo bash
```
```

---

## Teil 5: Implementierungsplan

### Phase 1: GitHub Actions (Tag 1-2)

| Task | Beschreibung | Aufwand |
|------|--------------|---------|
| 1.1 | `build-and-publish.yml` erstellen | 2 Std |
| 1.2 | Multi-Arch Build testen | 1 Std |
| 1.3 | Release-Job mit Changelog | 1 Std |
| 1.4 | Secrets in GitHub konfigurieren | 30 Min |
| 1.5 | Ersten Release (v0.1.0) taggen | 30 Min |

### Phase 2: Install-Skript (Tag 3-4)

| Task | Beschreibung | Aufwand |
|------|--------------|---------|
| 2.1 | `install.sh` schreiben | 3 Std |
| 2.2 | `update.sh` schreiben | 30 Min |
| 2.3 | `backup.sh` schreiben | 30 Min |
| 2.4 | Auf frischem Server testen | 2 Std |
| 2.5 | Edge Cases fixen | 1 Std |

### Phase 3: Dokumentation (Tag 5)

| Task | Beschreibung | Aufwand |
|------|--------------|---------|
| 3.1 | `INSTALL.md` schreiben | 1 Std |
| 3.2 | `.env.example` erweitern | 30 Min |
| 3.3 | README.md aktualisieren | 30 Min |
| 3.4 | `docker-compose.yml` vereinfachen | 1 Std |

### Phase 4: Testing & Finalisierung (Tag 6-7)

| Task | Beschreibung | Aufwand |
|------|--------------|---------|
| 4.1 | Frische VM Installation testen | 2 Std |
| 4.2 | Update-Prozess testen | 1 Std |
| 4.3 | Backup/Restore testen | 1 Std |
| 4.4 | Release v1.0.0 | 30 Min |

---

## Teil 6: Verifikation

### Erfolgskriterien

- [ ] Push auf `main` baut automatisch Images
- [ ] Tags erstellen GitHub Releases mit Changelog
- [ ] Images sind auf ghcr.io öffentlich verfügbar
- [ ] `install.sh` funktioniert auf frischer Ubuntu 22.04 VM
- [ ] SSL-Zertifikat wird automatisch bezogen
- [ ] Health-Check ist grün nach Installation
- [ ] Update-Prozess funktioniert ohne Datenverlust
- [ ] Backup-Skript erstellt vollständiges Backup

### Test-Szenarios

1. **Frische Installation**
   ```bash
   # Auf neuer VM
   curl -fsSL .../install.sh | sudo bash
   # → Mandari läuft unter https://domain.com
   ```

2. **Update**
   ```bash
   # Neue Version veröffentlicht
   curl -fsSL .../update.sh | sudo bash
   # → Neue Version läuft, Daten erhalten
   ```

3. **Backup & Restore**
   ```bash
   # Backup erstellen
   ./backup.sh
   # VM zerstören, neue erstellen
   ./install.sh
   # Backup einspielen
   docker compose exec -T postgres psql -U mandari < backup.sql
   ```

---

## Anhang: Benötigte GitHub Secrets

Für das öffentliche Repo (minimal):

| Secret | Beschreibung | Wert |
|--------|--------------|------|
| `GITHUB_TOKEN` | Automatisch vorhanden | - |

Das war's. Die Images werden mit dem automatischen `GITHUB_TOKEN` gepusht.

Für zusätzliche Notifications (optional):

| Secret | Beschreibung |
|--------|--------------|
| `DISCORD_WEBHOOK` | Release-Benachrichtigungen |
| `SLACK_WEBHOOK` | Release-Benachrichtigungen |
