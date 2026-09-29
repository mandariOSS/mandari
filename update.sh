#!/bin/bash
# =============================================================================
# Mandari - Zero-Downtime Update Script
# =============================================================================
# Updates Mandari ohne Ausfallzeit:
#   1. Images vorziehen (alter Container läuft weiter)
#   2. Worker (Ingestor) anhalten, Migrationen via temporären Container (vor dem Swap)
#   3. Container einzeln tauschen (Caddy puffert Requests)
#   4. Health-Check + automatisches Rollback bei Fehler
#   5. Nach den Post-Deploy-Migrationen Worker und Protokoll-Orchestrator mit neuem Image starten
#
# Usage:
#   ./update.sh                # Update auf latest
#   ./update.sh --tag v1.3.0   # Bestimmte Version
#   ./update.sh v1.3.0         # Bestimmte Version (Rückwärtskompatibilität)
#   ./update.sh --no-backup    # Backup überspringen
#   ./update.sh --no-cleanup   # Image-Cleanup überspringen
#   ./update.sh --rollback     # Auf vorherige Version zurück
#   ./update.sh --dry-run      # Nur prüfen, nichts ändern
# =============================================================================

set -euo pipefail

# Container-Namen folgen dem Compose-Projektnamen (siehe docker-compose.yml)
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-mandari}"
export COMPOSE_PROJECT_NAME
APP_CONTAINER="${COMPOSE_PROJECT_NAME}"
DB_CONTAINER="${COMPOSE_PROJECT_NAME}-postgres"
WEBSITE_CONTAINER="${COMPOSE_PROJECT_NAME}-website"

# Dienste, die während der Migrationen stehen (wie WORKER_SERVICES in deploy/scripts/deploy.sh).
# Der Ingestor und der Protokoll-Orchestrator – und ein getrennter OCR-Worker, falls in einer
# eigenen Compose-Datei betrieben – schreiben in Tabellen, die Migrationen ändern; liefen sie
# weiter, bräche ein Durchlauf ab. Überschreibbar per Umgebung oder .env, z. B.
# WORKER_SERVICES="ingestor minutes-orchestrator ocr-worker". Dienste, die Compose nicht kennt,
# werden übersprungen (bei der Vorgabe ohne Hinweis).
WORKER_SERVICES_DEFAULT="ingestor minutes-orchestrator"

# Dienste mit dem Anwendungs-Image, die nach den Migrationen immer auf das neue Image wechseln
# (Issue #479) – auch wenn ein eigenes WORKER_SERVICES sie nicht nennt; dann laufen sie während
# der Migrationen weiter. Nicht definierte Dienste werden übersprungen.
APP_IMAGE_SERVICES="minutes-orchestrator"

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
CYAN='\033[0;36m'
NC='\033[0m'

# State
ROLLBACK_IMAGES=()
UPDATE_FAILED=false

# Log file
mkdir -p "$SCRIPT_DIR/logs"
UPDATE_LOG="$SCRIPT_DIR/logs/update.log"

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

# Sicheres Lesen einzelner Werte aus .env (kein source = keine Code-Injection)
get_env_var() {
    local key="$1"
    local default="${2:-}"
    local val
    val=$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d'=' -f2-)
    echo "${val:-$default}"
}

# Run a command with spinner, output goes to log file
# Usage: run_step "Description" command arg1 arg2 ...
run_step() {
    local description="$1"
    shift

    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    # Start command in background, output to log
    "$@" >> "$UPDATE_LOG" 2>&1 &
    local pid=$!

    # Show spinner
    printf "  %-30s " "$description"
    while kill -0 "$pid" 2>/dev/null; do
        printf "\b${spin_chars:$i:1}"
        i=$(( (i + 1) % ${#spin_chars} ))
        sleep 0.1
    done

    # Check exit code
    wait "$pid"
    local exit_code=$?

    if [ $exit_code -eq 0 ]; then
        printf "\b${GREEN}✓${NC}\n"
    else
        printf "\b${RED}✗${NC}\n"
        warn "Fehlgeschlagen! Details: cat $UPDATE_LOG"
    fi

    return $exit_code
}

# Warte bis Container healthy ist (mit Spinner + Go-Template-Fix)
wait_for_healthy() {
    local container=$1
    local max_attempts=${2:-45}
    local attempt=0
    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    while [ $attempt -lt $max_attempts ]; do
        local status
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "unknown")

        if [ "$status" = "healthy" ]; then
            return 0
        fi

        if [ "$status" = "unhealthy" ]; then
            return 1
        fi

        # Container ohne Healthcheck gelten als OK wenn sie laufen
        if [ "$status" = "no-healthcheck" ]; then
            local running
            running=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "")
            if [ "$running" = "running" ]; then
                return 0
            fi
        fi

        attempt=$((attempt + 1))
        printf "\b${spin_chars:$i:1}"
        i=$(( (i + 1) % ${#spin_chars} ))
        sleep 2
    done

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
    local max_health_wait=${3:-45}

    local old_image
    old_image=$(get_current_image "$container")

    if [ -z "$old_image" ]; then
        warn "$container läuft nicht — starte neu"
        docker compose up -d --no-deps "$service" >> "$UPDATE_LOG" 2>&1
    else
        ROLLBACK_IMAGES+=("$service=$old_image")
        docker compose up -d --no-deps "$service" >> "$UPDATE_LOG" 2>&1
    fi

    printf "  %-30s " "$service"
    if wait_for_healthy "$container" "$max_health_wait"; then
        printf "\b${GREEN}✓ healthy${NC}\n"
        return 0
    else
        printf "\b${RED}✗ FAILED${NC}\n"
        return 1
    fi
}

# Rollback eines einzelnen Services auf vorheriges Image
rollback_service() {
    local service=$1
    local old_image=$2

    warn "Rollback: $service → $old_image"
    docker compose stop "$service" 2>/dev/null || true

    local container
    container=$(docker compose ps -q "$service" 2>/dev/null || echo "")
    if [ -n "$container" ]; then
        docker rm -f "$container" 2>/dev/null || true
    fi

    if [ -f ".env.pre-update" ]; then
        cp .env.pre-update .env
        docker compose up -d --no-deps "$service"
    fi
}

# Dienste aus der Liste $1, die Compose kennt – in der gegebenen Reihenfolge, ohne Doppelte.
# Mit $2 = warn wird jeder unbekannte Dienst gemeldet (Hinweise auf stderr, Ergebnis auf stdout).
defined_services() {
    local wanted="$1" mode="${2:-}" defined svc result=""
    wanted="${wanted//\"/}"
    wanted="${wanted//\'/}"
    defined=$(docker compose config --services 2>/dev/null || true)
    for svc in $wanted; do
        case " $result " in
            *" $svc "*) continue ;;
        esac
        if printf '%s\n' "$defined" | grep -qxF -- "$svc"; then
            result="$result $svc"
        elif [ "$mode" = "warn" ]; then
            warn "Dienst '$svc' aus WORKER_SERVICES ist nicht definiert – übersprungen." >&2
        fi
    done
    echo "${result# }"
}

# Worker, die während der Migrationen stehen: WORKER_SERVICES aus Umgebung oder .env,
# sonst die Vorgabe; beschränkt auf Dienste, die Compose kennt
resolve_workers() {
    local configured
    configured="${WORKER_SERVICES:-$(get_env_var WORKER_SERVICES "")}"
    if [ -n "$configured" ]; then
        defined_services "$configured" warn
    else
        defined_services "$WORKER_SERVICES_DEFAULT"
    fi
}

# Angehaltene Worker; start_workers startet sie (mit dem dann gültigen Image) wieder –
# auch beim Abbruch des Skripts (trap), damit kein Worker dauerhaft steht.
STOPPED_WORKERS=""
# Von start_workers bereits mit dem neuen Image gestartete Worker
STARTED_WORKERS=""

stop_workers() {
    local workers
    workers=$(resolve_workers)
    if [ -z "$workers" ]; then
        info "  Keine Worker anzuhalten"
        return 0
    fi
    STOPPED_WORKERS="$workers"
    # shellcheck disable=SC2086  # Dienstliste bewusst aufgeteilt
    run_step "Worker anhalten ($workers)" docker compose stop $workers || \
        error "Worker konnten nicht angehalten werden. Update abgebrochen, keine Migration eingespielt."
}

start_workers() {
    [ -n "$STOPPED_WORKERS" ] || return 0
    local workers="$STOPPED_WORKERS"
    STOPPED_WORKERS=""
    STARTED_WORKERS="$workers"
    # shellcheck disable=SC2086  # Dienstliste bewusst aufgeteilt
    if docker compose up -d --no-deps $workers >> "$UPDATE_LOG" 2>&1; then
        printf "  %-30s ${GREEN}✓${NC}\n" "$workers"
    else
        printf "  %-30s ${RED}✗${NC}\n" "$workers"
        warn "Worker nicht gestartet — von Hand: docker compose up -d --no-deps $workers"
    fi
}

# APP_IMAGE_SERVICES, die nicht schon als Worker gestartet wurden, auf das neue Image bringen
update_app_image_services() {
    local svc
    for svc in $(defined_services "$APP_IMAGE_SERVICES"); do
        case " $STARTED_WORKERS " in
            *" $svc "*) continue ;;
        esac
        if docker compose up -d --no-deps "$svc" >> "$UPDATE_LOG" 2>&1; then
            printf "  %-30s ${GREEN}✓${NC}\n" "$svc"
        else
            printf "  %-30s ${RED}✗${NC}\n" "$svc"
            warn "$svc nicht aktualisiert — von Hand: docker compose up -d --no-deps $svc"
        fi
    done
}

is_stopped_worker() {
    case " $STOPPED_WORKERS " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

trap start_workers EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Verifikation aller Container (wie install.sh)
verify_installation() {
    log "Verifikation..."
    echo ""

    local all_ok=true

    for container in mandari-postgres mandari-redis mandari-elasticsearch mandari mandari-website mandari-caddy mandari-ingestor; do
        local status
        local health
        status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
        health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "no-healthcheck")

        local label
        case "$container" in
            mandari-postgres)    label="PostgreSQL" ;;
            mandari-redis)       label="Redis" ;;
            mandari-elasticsearch) label="Elasticsearch" ;;
            mandari)             label="Mandari" ;;
            mandari-website)     label="Website" ;;
            mandari-caddy)       label="Caddy" ;;
            mandari-ingestor)    label="Ingestor" ;;
        esac

        printf "  %-14s " "$label"
        if [ "$status" = "running" ]; then
            if [ "$health" = "healthy" ]; then
                echo -e "${GREEN}✓ healthy${NC}"
            elif [ "$health" = "no-healthcheck" ] || [ -z "$health" ] || [ "$health" = "none" ]; then
                echo -e "${GREEN}✓ running${NC}"
            elif [ "$health" = "starting" ]; then
                echo -e "${YELLOW}⏳ starting${NC}"
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
    local domain
    domain=$(get_env_var DOMAIN localhost)
    if [ "$domain" != "localhost" ]; then
        printf "  %-14s " "HTTPS"
        if command -v curl &>/dev/null; then
            local http_code
            http_code=$(curl -sk -o /dev/null -w '%{http_code}' "https://${domain}/health/" 2>/dev/null || echo "000")
            if [ "$http_code" = "200" ]; then
                echo -e "${GREEN}✓ https://${domain} erreichbar${NC}"
            elif [ "$http_code" = "000" ]; then
                echo -e "${YELLOW}⚠ Nicht erreichbar${NC}"
            else
                echo -e "${YELLOW}⚠ HTTP $http_code${NC}"
            fi
        fi
        echo ""
    fi

    if [ "$all_ok" = "false" ]; then
        echo ""
        warn "Einige Services sind nicht healthy."
        echo ""
        echo "  Fehlerbehebung:"
        echo "    Logs ansehen:     docker compose logs -f <service>"
        echo "    Neustart:         docker compose restart <service>"
        echo "    Status prüfen:    docker compose ps"
        echo ""
    fi
}

# Banner
show_banner() {
    echo -e "${CYAN}"
    cat << "EOF"
  __  __                 _            _
 |  \/  | __ _ _ __   __| | __ _ _ __(_)
 | |\/| |/ _` | '_ \ / _` |/ _` | '__| |
 | |  | | (_| | | | | (_| | (_| | |  | |
 |_|  |_|\__,_|_| |_|\__,_|\__,_|_|  |_|

 Zero-Downtime Update

EOF
    echo -e "${NC}"
}

# =============================================================================
# Parse Arguments
# =============================================================================
TARGET_VERSION=""
SKIP_BACKUP=false
SKIP_CLEANUP=false
DRY_RUN=false
DO_ROLLBACK=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)
            if [ -z "${2:-}" ]; then
                error "--tag benötigt einen Wert (z.B. --tag v1.3.0)"
            fi
            TARGET_VERSION="$2"
            shift 2
            ;;
        --tag=*)
            TARGET_VERSION="${1#--tag=}"
            shift
            ;;
        --no-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --no-cleanup)
            SKIP_CLEANUP=true
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
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --tag VERSION   Zielversion (z.B. v1.3.0, dev, latest)"
            echo "  --no-backup     Backup überspringen"
            echo "  --no-cleanup    Image-Cleanup überspringen"
            echo "  --dry-run       Nur prüfen, nichts ändern"
            echo "  --rollback      Auf vorherige Version zurückrollen"
            echo "  -h, --help      Diese Hilfe anzeigen"
            exit 0
            ;;
        -*)
            error "Unbekannte Option: $1\nUsage: $0 --help"
            ;;
        *)
            # Rückwärtskompatibilität: positionales Argument als Version
            TARGET_VERSION="$1"
            shift
            ;;
    esac
done

# =============================================================================
# Pre-flight Checks
# =============================================================================
show_banner

# .env vorhanden?
if [ ! -f ".env" ]; then
    error "Keine .env Datei gefunden. Ist Mandari installiert?"
fi

# Docker läuft?
if ! docker info &>/dev/null; then
    error "Docker Daemon läuft nicht"
fi

# Log-Datei initialisieren
echo "=== Mandari Update $(date) ===" > "$UPDATE_LOG"

# Aktuelle Version merken (sicheres Parsing, kein source)
CURRENT_VERSION=$(get_env_var IMAGE_TAG latest)
info "Aktuelle Version: ${CYAN}$CURRENT_VERSION${NC}"

# =============================================================================
# Rollback Mode
# =============================================================================
if [ "$DO_ROLLBACK" = true ]; then
    if [ ! -f ".env.pre-update" ]; then
        error "Kein .env.pre-update gefunden. Kein Rollback möglich."
    fi

    warn "Rollback auf vorherige Version..."
    cp .env.pre-update .env
    ROLLBACK_VERSION=$(get_env_var IMAGE_TAG latest)
    info "Zielversion: ${CYAN}$ROLLBACK_VERSION${NC}"

    run_step "Images herunterladen" docker compose pull -q mandari website ingestor || true

    log "Container tauschen..."
    swap_container mandari mandari 45 || true
    swap_container website mandari-website 30 || true

    # Ingestor, Worker und übrige Dienste mit dem Anwendungs-Image ebenfalls zurück
    rollback_services=$(defined_services "ingestor $(resolve_workers) $APP_IMAGE_SERVICES")
    if [ -n "$rollback_services" ]; then
        # shellcheck disable=SC2086  # Dienstliste bewusst aufgeteilt
        docker compose up -d --no-deps $rollback_services >> "$UPDATE_LOG" 2>&1
        printf "  %-30s ${GREEN}✓${NC}\n" "$rollback_services"
    fi

    echo ""
    verify_installation

    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Rollback abgeschlossen!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  Version:   ${CYAN}$ROLLBACK_VERSION${NC}"
    echo "  Logs:      cat $UPDATE_LOG"
    echo ""
    exit 0
fi

# =============================================================================
# Dry Run
# =============================================================================
if [ "$DRY_RUN" = true ]; then
    info "Dry-Run Modus — keine Änderungen"
    echo ""

    if [ -n "$TARGET_VERSION" ]; then
        info "Zielversion: ${CYAN}$TARGET_VERSION${NC}"
    else
        info "Zielversion: ${CYAN}latest${NC}"
    fi

    dry_run_workers=$(resolve_workers)
    info "Während der Migrationen angehalten: ${CYAN}${dry_run_workers:-keine}${NC}"
    dry_run_app_services=$(defined_services "$APP_IMAGE_SERVICES")
    info "Nach den Migrationen auf das neue Image: ${CYAN}${dry_run_app_services:-keine}${NC}"

    echo ""
    verify_installation

    log "Dry-Run abgeschlossen. Keine Änderungen vorgenommen."
    exit 0
fi

# =============================================================================
# Backup
# =============================================================================
if [ "$SKIP_BACKUP" = false ]; then
    if [ -x "./backup.sh" ]; then
        warn "Backup wird empfohlen vor dem Update."
        read -p "Backup jetzt erstellen? [Y/n]: " do_backup
        if [[ ! "$do_backup" =~ ^[Nn]$ ]]; then
            log "Erstelle Backup..."
            ./backup.sh
            echo ""
        fi
    else
        warn "backup.sh nicht gefunden. Backup übersprungen."
    fi
else
    info "Backup übersprungen (--no-backup)"
fi

# =============================================================================
# Update-Konfiguration sichern
# =============================================================================
cp .env .env.pre-update

if [ -n "$TARGET_VERSION" ]; then
    info "Zielversion: ${CYAN}$TARGET_VERSION${NC}"
    if grep -q "^IMAGE_TAG=" .env; then
        sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=$TARGET_VERSION/" .env
    else
        echo "IMAGE_TAG=$TARGET_VERSION" >> .env
    fi
    rm -f .env.bak
else
    info "Zielversion: ${CYAN}latest${NC}"
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
    git fetch origin >> "$UPDATE_LOG" 2>&1
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ -n "$current_branch" ] && [ "$current_branch" != "HEAD" ]; then
        git pull origin "$current_branch" >> "$UPDATE_LOG" 2>&1 || warn "Git pull fehlgeschlagen. Docker-Update läuft weiter."
    fi
fi

# =============================================================================
# Phase 1: Images vorziehen
# =============================================================================
echo ""
log "Phase 1: Images"
run_step "Neue Images herunterladen" docker compose pull -q

# =============================================================================
# Phase 2: Pre-Deploy Migrationen
# =============================================================================
echo ""
log "Phase 2: Pre-Deploy Migrationen"
info "  (Temporärer Container, alter bedient weiter Requests)"

# Worker stehen bis nach den Post-Deploy-Migrationen (Phase 4): Kein Sync-Zyklus läuft gegen
# ein Schema, das sich gerade ändert. Für Besucher unsichtbar.
stop_workers

run_step "Pre-Deploy Migrationen" docker compose run --rm --no-deps \
    mandari python manage.py safemigrate --noinput || \
    warn "  Pre-Deploy Migrationen fehlgeschlagen (möglicherweise bereits aktuell)"

run_step "Website-Migrationen" docker compose run --rm --no-deps \
    website python manage.py migrate --noinput || \
    warn "  Website-Migrationen fehlgeschlagen oder nicht nötig"

# =============================================================================
# Phase 3: Container-Swap (Caddy puffert ~3-5s pro Service)
# =============================================================================
echo ""
log "Phase 3: Container tauschen"
info "  (Caddy puffert Requests während des Swaps)"

# Mandari (Django Backend)
if ! swap_container mandari mandari 45; then
    UPDATE_FAILED=true
    warn "Mandari-Container unhealthy nach Swap!"
    warn "Versuche Rollback..."

    if [ -f ".env.pre-update" ]; then
        cp .env.pre-update .env
        docker compose up -d --no-deps mandari >> "$UPDATE_LOG" 2>&1
        printf "  %-30s " "mandari (rollback)"
        if wait_for_healthy mandari 45; then
            printf "\b${GREEN}✓ restored${NC}\n"
        else
            printf "\b${RED}✗ FAILED${NC}\n"
        fi
    fi

    error "Update fehlgeschlagen. Mandari auf vorherige Version zurückgesetzt. Prüfe: docker logs $APP_CONTAINER"
fi

# Anwendungsprüfung (Issue #285): "healthy" heißt nur, dass der Prozess antwortet.
# Erst wenn Anmeldeseite, Portal und API mit Inhalt kommen, gilt das Update als gelungen;
# sonst Rollback wie oben.
if [ -f "$SCRIPT_DIR/deploy/scripts/verify_deploy.py" ]; then
    printf "  %-30s " "Anwendungsprüfung"
    docker cp "$SCRIPT_DIR/deploy/scripts/verify_deploy.py" "$APP_CONTAINER:/tmp/verify_deploy.py" >> "$UPDATE_LOG" 2>&1
    if docker exec "$APP_CONTAINER" python manage.py shell -c "exec(open('/tmp/verify_deploy.py').read())" >> "$UPDATE_LOG" 2>&1; then
        printf "${GREEN}✓ bestanden${NC}
"
    else
        printf "${RED}✗ FAILED${NC}
"
        UPDATE_FAILED=true
        warn "Anwendungsprüfung fehlgeschlagen (Details: $UPDATE_LOG). Versuche Rollback..."
        if [ -f ".env.pre-update" ]; then
            cp .env.pre-update .env
            docker compose up -d --no-deps mandari >> "$UPDATE_LOG" 2>&1
            printf "  %-30s " "mandari (rollback)"
            if wait_for_healthy mandari 45; then
                printf "${GREEN}✓ restored${NC}
"
            else
                printf "${RED}✗ FAILED${NC}
"
            fi
        fi
        error "Update fehlgeschlagen. Mandari auf vorherige Version zurückgesetzt. Prüfe: $UPDATE_LOG"
    fi
fi

# Website (Wagtail)
if ! swap_container website mandari-website 30; then
    warn "Website-Container unhealthy — prüfe Logs: docker logs $WEBSITE_CONTAINER"
fi

# Ingestor (kein User-Impact); als angehaltener Worker startet er erst nach Phase 4
if ! is_stopped_worker ingestor; then
    docker compose up -d --no-deps ingestor >> "$UPDATE_LOG" 2>&1
    printf "  %-30s ${GREEN}✓${NC}\n" "ingestor"
fi

# =============================================================================
# Phase 4: Post-Deploy Migrationen
# =============================================================================
echo ""
log "Phase 4: Post-Deploy Migrationen"

run_step "Post-Deploy Migrationen" docker exec "$APP_CONTAINER" python manage.py migrate --noinput || \
    warn "  Post-Deploy Migrationen fehlgeschlagen — prüfe: docker logs $APP_CONTAINER"

# =============================================================================
# Phase 5: Worker mit neuem Image starten (nach allen Migrationen)
# =============================================================================
echo ""
log "Phase 5: Worker starten"
start_workers
update_app_image_services

# =============================================================================
# Phase 6: Caddy reload (falls Caddyfile geändert)
# =============================================================================
echo ""
log "Phase 6: Caddy-Konfiguration"

run_step "Caddy reload" docker exec "${COMPOSE_PROJECT_NAME}-caddy" caddy reload --config /etc/caddy/Caddyfile || \
    info "  Caddy-Reload übersprungen (kein Reload nötig oder Caddy nicht verfügbar)"

# =============================================================================
# Phase 7: Verifikation
# =============================================================================
echo ""
log "Phase 7: Verifikation"
sleep 3

verify_installation

# =============================================================================
# Cleanup
# =============================================================================
if [ "$SKIP_CLEANUP" = false ]; then
    run_step "Alte Images aufräumen" docker image prune -f
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Update abgeschlossen!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  Version:   ${CYAN}$(get_env_var IMAGE_TAG latest)${NC}"
echo "  Rollback:  ./update.sh --rollback"
echo "  Logs:      cat $UPDATE_LOG"
echo ""
