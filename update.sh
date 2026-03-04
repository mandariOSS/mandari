#!/bin/bash
# =============================================================================
# Mandari - Zero-Downtime Update Script
# =============================================================================
# Updates Mandari ohne Ausfallzeit:
#   1. Images vorziehen (alter Container läuft weiter)
#   2. Migrationen via temporären Container (vor dem Swap)
#   3. Container einzeln tauschen (Caddy puffert Requests)
#   4. Health-Check + automatisches Rollback bei Fehler
#
# Usage:
#   ./update.sh              # Update auf latest
#   ./update.sh v1.3.0       # Bestimmte Version
#   ./update.sh --no-backup  # Backup überspringen
#   ./update.sh --rollback   # Auf vorherige Version zurück
#   ./update.sh --dry-run    # Nur prüfen, nichts ändern
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

# State
ROLLBACK_IMAGES=()
UPDATE_FAILED=false

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

# Warte bis Container healthy ist
wait_for_healthy() {
    local container=$1
    local max_seconds=${2:-90}
    local elapsed=0

    while [ $elapsed -lt $max_seconds ]; do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")

        if [ "$status" = "healthy" ]; then
            return 0
        fi

        if [ "$status" = "unhealthy" ]; then
            return 1
        fi

        elapsed=$((elapsed + 2))
        printf "."
        sleep 2
    done

    echo ""
    return 1
}

# Aktuelles Image eines Containers speichern
get_current_image() {
    local container=$1
    docker inspect --format='{{.Config.Image}}' "$container" 2>/dev/null || echo ""
}

# Container-Swap: einzelnen Service mit neuem Image starten
swap_container() {
    local service=$1
    local container=$2
    local max_health_wait=${3:-90}

    local old_image
    old_image=$(get_current_image "$container")

    if [ -z "$old_image" ]; then
        warn "$container läuft nicht — starte neu"
        docker compose up -d --no-deps "$service"
    else
        log "Tausche $service (Caddy puffert Requests)..."
        ROLLBACK_IMAGES+=("$service=$old_image")
        docker compose up -d --no-deps "$service"
    fi

    echo -n "  $service"
    if wait_for_healthy "$container" "$max_health_wait"; then
        echo -e " ${GREEN}healthy${NC}"
        return 0
    else
        echo -e " ${RED}FAILED${NC}"
        return 1
    fi
}

# Rollback eines einzelnen Services auf vorheriges Image
rollback_service() {
    local service=$1
    local old_image=$2

    warn "Rollback: $service → $old_image"
    docker compose stop "$service" 2>/dev/null || true

    # Service mit altem Image über environment override starten
    # Wir nutzen docker run direkt, da compose das neue Image nutzen würde
    local container
    container=$(docker compose ps -q "$service" 2>/dev/null || echo "")
    if [ -n "$container" ]; then
        docker rm -f "$container" 2>/dev/null || true
    fi

    # Altes Image-Tag in .env zurücksetzen
    if [ -f ".env.pre-update" ]; then
        cp .env.pre-update .env
        docker compose up -d --no-deps "$service"
    fi
}

# =============================================================================
# Parse Arguments
# =============================================================================
TARGET_VERSION=""
SKIP_BACKUP=false
DRY_RUN=false
DO_ROLLBACK=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --rollback)
            DO_ROLLBACK=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [VERSION] [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  VERSION       Target version (z.B. v1.3.0). Default: 'latest'"
            echo "  --no-backup   Backup überspringen"
            echo "  --dry-run     Nur prüfen, nichts ändern"
            echo "  --rollback    Auf vorherige Version zurückrollen"
            echo "  -h, --help    Diese Hilfe anzeigen"
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

# .env vorhanden?
if [ ! -f ".env" ]; then
    error "Keine .env Datei gefunden. Ist Mandari installiert?"
fi

# Docker läuft?
if ! docker info &>/dev/null; then
    error "Docker Daemon läuft nicht"
fi

# Aktuelle Version merken
source .env
CURRENT_VERSION="${IMAGE_TAG:-latest}"
log "Aktuelle Version: $CURRENT_VERSION"

# =============================================================================
# Rollback Mode
# =============================================================================
if [ "$DO_ROLLBACK" = true ]; then
    if [ ! -f ".env.pre-update" ]; then
        error "Kein .env.pre-update gefunden. Kein Rollback möglich."
    fi

    warn "Rollback auf vorherige Version..."
    cp .env.pre-update .env
    source .env
    log "Zielversion: ${IMAGE_TAG:-latest}"

    docker compose pull mandari website ingestor 2>/dev/null || true

    log "Container tauschen..."
    docker compose up -d --no-deps mandari
    echo -n "  mandari"
    wait_for_healthy mandari 90 && echo -e " ${GREEN}healthy${NC}" || echo -e " ${YELLOW}starting${NC}"

    docker compose up -d --no-deps website
    echo -n "  website"
    wait_for_healthy mandari-website 60 && echo -e " ${GREEN}healthy${NC}" || echo -e " ${YELLOW}starting${NC}"

    docker compose up -d --no-deps ingestor
    log "Rollback abgeschlossen!"
    exit 0
fi

# =============================================================================
# Dry Run
# =============================================================================
if [ "$DRY_RUN" = true ]; then
    info "Dry-Run Modus — keine Änderungen"
    echo ""

    log "Aktueller Status:"
    docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || docker compose ps

    echo ""
    if [ -n "$TARGET_VERSION" ]; then
        log "Zielversion: $TARGET_VERSION"
    else
        log "Zielversion: latest"
    fi

    echo ""
    log "Prüfe ob neue Images verfügbar sind..."
    docker compose pull --dry-run 2>/dev/null || docker compose pull --quiet 2>&1 | head -5 || info "Pull-Check nicht unterstützt — docker compose pull wird beim echten Update ausgeführt"

    echo ""
    log "Dry-Run abgeschlossen. Keine Änderungen vorgenommen."
    exit 0
fi

# =============================================================================
# Backup
# =============================================================================
if [ "$SKIP_BACKUP" = false ]; then
    echo ""
    if [ -x "./backup.sh" ]; then
        warn "Backup wird empfohlen vor dem Update."
        read -p "Backup jetzt erstellen? [Y/n]: " do_backup
        if [[ ! "$do_backup" =~ ^[Nn]$ ]]; then
            log "Erstelle Backup..."
            ./backup.sh
        fi
    else
        warn "backup.sh nicht gefunden. Backup übersprungen."
    fi
else
    warn "Backup übersprungen (--no-backup)"
fi

# =============================================================================
# Update-Konfiguration sichern
# =============================================================================
cp .env .env.pre-update

if [ -n "$TARGET_VERSION" ]; then
    log "Zielversion: $TARGET_VERSION"
    if grep -q "^IMAGE_TAG=" .env; then
        sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=$TARGET_VERSION/" .env
    else
        echo "IMAGE_TAG=$TARGET_VERSION" >> .env
    fi
    rm -f .env.bak
else
    log "Zielversion: latest"
    if grep -q "^IMAGE_TAG=" .env; then
        sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=latest/" .env
        rm -f .env.bak
    fi
fi

# =============================================================================
# Git Pull (wenn Repo vorhanden)
# =============================================================================
if [ -d ".git" ]; then
    log "Git-Repository aktualisieren..."
    git fetch origin
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ -n "$current_branch" ] && [ "$current_branch" != "HEAD" ]; then
        git pull origin "$current_branch" || warn "Git pull fehlgeschlagen. Docker-Update läuft weiter."
    fi
fi

# =============================================================================
# Phase 1: Images vorziehen (alter Container läuft weiter!)
# =============================================================================
echo ""
log "Phase 1: Neue Images herunterladen..."
info "  (Alter Container bedient weiterhin Requests)"
docker compose pull

# =============================================================================
# Phase 2: Migrationen BEVOR Container getauscht werden
# =============================================================================
echo ""
log "Phase 2: Datenbank-Migrationen..."
info "  (Temporärer Container, alter bedient weiter Requests)"

# Mandari-Migrationen via temporären Container
if docker compose run --rm --no-deps \
    -e DATABASE_URL="postgresql://${POSTGRES_USER:-mandari}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-mandari}" \
    mandari python manage.py migrate --noinput 2>&1; then
    log "  Mandari-Migrationen erfolgreich"
else
    warn "  Mandari-Migrationen fehlgeschlagen — prüfe Logs"
    warn "  Update wird fortgesetzt (Migrationen könnten bereits aktuell sein)"
fi

# Website-Migrationen via temporären Container
if docker compose run --rm --no-deps \
    website python manage.py migrate --noinput 2>&1; then
    log "  Website-Migrationen erfolgreich"
else
    warn "  Website-Migrationen fehlgeschlagen oder nicht nötig"
fi

# =============================================================================
# Phase 3: Container-Swap (Caddy puffert ~3-5s pro Service)
# =============================================================================
echo ""
log "Phase 3: Container tauschen..."
info "  (Caddy puffert Requests während des Swaps)"

# Mandari (Django Backend)
if ! swap_container mandari mandari 90; then
    UPDATE_FAILED=true
    warn "Mandari-Container unhealthy nach Swap!"
    warn "Versuche Rollback..."

    if [ -f ".env.pre-update" ]; then
        cp .env.pre-update .env
        docker compose up -d --no-deps mandari
        echo -n "  mandari (rollback)"
        if wait_for_healthy mandari 90; then
            echo -e " ${GREEN}restored${NC}"
        else
            echo -e " ${RED}FAILED${NC}"
        fi
    fi

    error "Update fehlgeschlagen. Mandari auf vorherige Version zurückgesetzt. Prüfe: docker logs mandari"
fi

# Website (Wagtail)
if ! swap_container website mandari-website 60; then
    warn "Website-Container unhealthy — prüfe Logs: docker logs mandari-website"
fi

# Ingestor (kein User-Impact)
log "Ingestor aktualisieren..."
docker compose up -d --no-deps ingestor
log "  Ingestor gestartet"

# =============================================================================
# Phase 4: Caddy reload (falls Caddyfile geändert)
# =============================================================================
echo ""
log "Phase 4: Caddy-Konfiguration prüfen..."
if docker exec mandari-caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null; then
    log "  Caddy-Konfiguration neu geladen"
else
    info "  Caddy-Reload übersprungen (kein Reload nötig oder Caddy nicht verfügbar)"
fi

# =============================================================================
# Phase 5: Abschließende Verifikation
# =============================================================================
echo ""
log "Phase 5: Verifikation..."
sleep 3

echo ""
log "Service-Status:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || docker compose ps

echo ""
log "Health-Checks:"
if docker exec mandari curl -sf http://localhost:8000/health/ &>/dev/null; then
    echo -e "  Mandari:  ${GREEN}healthy${NC}"
else
    echo -e "  Mandari:  ${YELLOW}starting...${NC}"
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
read -p "Alte Docker-Images aufräumen? [y/N]: " cleanup
if [[ "$cleanup" =~ ^[Yy]$ ]]; then
    log "Alte Images entfernen..."
    docker image prune -f
fi

# =============================================================================
# Fertig
# =============================================================================
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Update abgeschlossen!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
source .env
echo "  Version:   ${IMAGE_TAG:-latest}"
echo "  Rollback:  ./update.sh --rollback"
echo ""
echo "  Logs:      docker compose logs -f"
echo "  Status:    docker compose ps"
echo ""
