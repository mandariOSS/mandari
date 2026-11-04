#!/bin/bash
# =============================================================================
# Mandari - Community Edition Installer
# =============================================================================
# Interactive installer for single-server deployment
#
# Usage:
#   ./install.sh                    # Interactive mode (latest)
#   ./install.sh --tag v0.11.0      # Install a released version (https://github.com/mandariOSS/mandari/releases)
#   ./install.sh --tag dev          # Install the development state (branch dev, unstable)
#   ./install.sh --unattended       # Use defaults or environment variables
#                                   # (aborts if an installation already exists)
#   ./install.sh --unattended --reinstall-destroy-data
#                                   # Replace an existing installation – DELETES ALL DATA
#   ./install.sh --help             # Show all options
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Compose-Projektname: bestimmt Container- und Volume-Namen. Mehrere Installationen auf
# einem Host brauchen unterschiedliche Namen.
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-mandari}"
export COMPOSE_PROJECT_NAME

# Container-Namen leiten sich aus dem Projektnamen ab (siehe docker-compose.yml)
APP_CONTAINER="${COMPOSE_PROJECT_NAME}"
DB_CONTAINER="${COMPOSE_PROJECT_NAME}-postgres"
REDIS_CONTAINER="${COMPOSE_PROJECT_NAME}-redis"
SEARCH_CONTAINER="${COMPOSE_PROJECT_NAME}-elasticsearch"
WEBSITE_CONTAINER="${COMPOSE_PROJECT_NAME}-website"
PROXY_CONTAINER="${COMPOSE_PROJECT_NAME}-caddy"
WORKER_CONTAINER="${COMPOSE_PROJECT_NAME}-worker"
WORKER_HEAVY_CONTAINER="${COMPOSE_PROJECT_NAME}-worker-heavy"
WORKER_LIVE_CONTAINER="${COMPOSE_PROJECT_NAME}-worker-live"

# Passwortrichtlinie der Anwendung (AUTH_PASSWORD_VALIDATORS in mandari/settings.py): mindestens 12 Zeichen.
# createsuperuser --noinput prüft das nicht, deshalb prüft der Installer selbst (Issue #962).
ADMIN_PASSWORD_MIN_LENGTH=12

# Passphrase, mit der backup.sh die .env verschlüsselt mitsichert (Vorgabe von backup.sh, Issue #962)
BACKUP_PASSPHRASE_DEFAULT_FILE=".backup-passphrase"

# Anwendungs-Images (siehe docker-compose.yml): Alle drei laufen mit demselben IMAGE_TAG
APP_IMAGES="ghcr.io/mandarioss/mandari ghcr.io/mandarioss/ingestor ghcr.io/mandarioss/website"
RELEASES_URL="https://github.com/mandariOSS/mandari/releases"

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

# Install log file
INSTALL_LOG="$SCRIPT_DIR/logs/install.log"

# Run a command with spinner, output goes to log file
# Usage: run_step "Description" command arg1 arg2 ...
run_step() {
    local description="$1"
    shift

    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

    # Start command in background, output to log
    "$@" >> "$INSTALL_LOG" 2>&1 &
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
        warn "Fehlgeschlagen! Details: cat $INSTALL_LOG"
    fi

    return $exit_code
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

# Platzhalteradresse? Let's Encrypt lehnt Adressen unter example.com/.org/.net und localhost ab (Issue #962)
is_placeholder_email() {
    case "${1,,}" in
        *@example.com|*@example.org|*@example.net|*@*.example.com|*@*.example.org|*@*.example.net|*@localhost) return 0 ;;
    esac
    return 1
}

# Adresse für Let's Encrypt übernehmen: Platzhalter fallen weg (Caddy arbeitet dann ohne Adresse)
accept_acme_email() {
    if [ -n "$ACME_EMAIL" ] && is_placeholder_email "$ACME_EMAIL"; then
        warn "ACME_EMAIL=$ACME_EMAIL ist eine Platzhalteradresse; Let's Encrypt lehnt sie ab. Zertifikate ohne Adresse."
        ACME_EMAIL=""
    fi
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
 Open Source unter AGPL-3.0-or-later

EOF
    echo -e "${NC}"
}

# =============================================================================
# Image-Tag prüfen (Issue #698)
# =============================================================================
# Gibt es das Image? 0 = vorhanden, 1 = fehlt, 2 = nicht prüfbar (Registry nicht erreichbar o. Ä.).
# "docker manifest inspect" lädt nichts herunter; ältere Docker-Versionen ohne den Befehl entscheiden
# per "docker pull".
image_status() {
    local ref="$1" ausgabe
    if ausgabe=$(docker manifest inspect "$ref" 2>&1); then
        return 0
    fi
    # Here-String statt Pipe: grep -q beendet früh, unter pipefail zählte sonst der Abbruch von printf (#695)
    if grep -qiE 'no such manifest|manifest unknown|manifest .*not found' <<< "$ausgabe"; then
        return 1
    fi
    if docker pull -q "$ref" >/dev/null 2>&1; then
        return 0
    fi
    if grep -qiE 'not found|manifest unknown' <<< "$ausgabe"; then
        return 1
    fi
    return 2
}

# Bricht mit verständlicher Meldung ab, wenn für das Tag nicht alle Anwendungs-Images abrufbar sind –
# bevor irgendetwas angelegt, überschrieben oder gelöscht wird.
check_image_tag() {
    local tag="$1" image rc fehlend="" unklar=""
    log "Images für Version „$tag“ prüfen..."
    for image in $APP_IMAGES; do
        rc=0
        image_status "$image:$tag" || rc=$?
        case $rc in
            0) printf "  %-40s ${GREEN}✓${NC}\n" "${image##*/}:$tag" ;;
            1) printf "  %-40s ${RED}fehlt${NC}\n" "${image##*/}:$tag"; fehlend="$fehlend ${image##*/}" ;;
            *) printf "  %-40s ${YELLOW}nicht abrufbar${NC}\n" "${image##*/}:$tag"; unklar="$unklar ${image##*/}" ;;
        esac
    done
    if [ -n "$fehlend" ]; then
        error "Für die Version „$tag“ gibt es nicht alle Images (fehlt:$fehlend). Es wurde nichts verändert.

  Mögliche Werte für --tag:
    latest     neuester stabiler Stand (Vorgabe)
    dev        Entwicklungsstand (Zweig dev, instabil, nicht für den Produktivbetrieb)
    v0.11.0    eine veröffentlichte Version mit führendem „v“, siehe $RELEASES_URL"
    fi
    if [ -n "$unklar" ]; then
        error "Die Images für „$tag“ sind nicht abrufbar ($unklar). Es wurde nichts verändert.
  Ist ghcr.io von diesem Server erreichbar? Test: docker pull ghcr.io/mandarioss/mandari:$tag"
    fi
    IMAGE_TAG_GEPRUEFT="$tag"
}

# Interaktiv ohne --tag: Release-Kanal abfragen
choose_image_tag() {
    echo ""
    echo -e "  ${CYAN}Release-Kanal wählen:${NC}"
    echo "    1) latest  — neuester stabiler Stand (empfohlen für Produktion)"
    echo "    2) dev     — Entwicklungsstand aus dem Zweig dev (instabil, nicht für Produktion)"
    echo ""
    echo "  Eine bestimmte Version (z. B. v0.11.0) mit: ./install.sh --tag v0.11.0"
    echo ""
    read -p "  Auswahl [1]: " channel_choice
    case "${channel_choice:-1}" in
        2) IMAGE_TAG="dev" ;;
        *) IMAGE_TAG="latest" ;;
    esac
}

# Version festlegen (interaktiv ggf. per Kanalwahl) und prüfen, falls noch nicht geschehen.
# Läuft spätestens vor dem ersten Schreiben und vor jedem Löschen von Containern oder Volumes.
ensure_image_tag() {
    if [ -z "$IMAGE_TAG" ]; then
        choose_image_tag
    fi
    if [ "$IMAGE_TAG_GEPRUEFT" != "$IMAGE_TAG" ]; then
        check_image_tag "$IMAGE_TAG"
    fi
}

# =============================================================================
# Prerequisites Check
# =============================================================================
check_prerequisites() {
    log "Voraussetzungen prüfen..."

    # sudo nur verwenden, wenn wir nicht ohnehin root sind
    SUDO=""
    if [ "$EUID" -ne 0 ]; then
        if command -v sudo &>/dev/null; then
            SUDO="sudo"
        else
            warn "Weder root noch sudo verfügbar – Systemänderungen können fehlschlagen."
        fi
    fi

    # Docker installieren, falls nicht vorhanden. Das System selbst wird bewusst NICHT
    # aktualisiert – ein Installer darf keine unerwarteten Paket-Upgrades auslösen.
    if ! command -v docker &>/dev/null; then
        log "Docker nicht gefunden – wird installiert (https://get.docker.com)"
        if [ "$UNATTENDED" != "true" ]; then
            read -r -p "  Docker jetzt installieren? [J/n]: " install_docker
            if [[ "$install_docker" =~ ^[Nn]$ ]]; then
                error "Docker wird benötigt. Anleitung: https://docs.docker.com/engine/install/"
            fi
        fi
        if command -v curl &>/dev/null; then
            curl -fsSL https://get.docker.com | $SUDO sh
        elif command -v wget &>/dev/null; then
            wget -qO- https://get.docker.com | $SUDO sh
        else
            error "Weder curl noch wget verfügbar. Bitte Docker manuell installieren: https://docs.docker.com/engine/install/"
        fi

        if command -v systemctl &>/dev/null; then
            $SUDO systemctl enable --now docker || true
        fi

        if ! command -v docker &>/dev/null; then
            error "Docker-Installation fehlgeschlagen. Anleitung: https://docs.docker.com/engine/install/"
        fi
        log "Docker installiert"
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
            $SUDO systemctl start docker
            sleep 2
        fi
        if ! docker info &>/dev/null; then
            error "Docker-Daemon läuft nicht. Bitte starten: sudo systemctl start docker"
        fi
    fi

    local docker_version
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    log "Docker version: $docker_version"

    # Version schon bekannt (--tag oder unbeaufsichtigt): vor jeder Änderung prüfen, ob es sie gibt
    if [ -n "$IMAGE_TAG" ]; then
        check_image_tag "$IMAGE_TAG"
    fi

    # Läuft dieses Compose-Projekt bereits aus einem ANDEREN Verzeichnis? Container- und
    # Volume-Namen sind projektweit eindeutig; zwei Installationen würden sich sonst
    # gegenseitig die Daten überschreiben.
    local other_dir
    other_dir=$(docker ps -a --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
        --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | grep -v '^$' | head -1 || true)
    if [ -n "$other_dir" ] && [ "$other_dir" != "$SCRIPT_DIR" ]; then
        error "Es existiert bereits eine Installation „${COMPOSE_PROJECT_NAME}“ in: $other_dir
  Zwei Installationen mit demselben Projektnamen teilen sich Container und Daten.
  Entweder dort weiterarbeiten (cd \"$other_dir\" && ./update.sh) oder hier einen
  eigenen Namen setzen:  COMPOSE_PROJECT_NAME=mandari-zweit ./install.sh"
    fi

    # Datenbank-Volume ohne passende .env: Die Zugangsdaten im Volume stammen aus einer
    # früheren Installation, neu erzeugte Passwörter passen nicht dazu (Anmeldefehler).
    if [ ! -f ".env" ] && docker volume inspect "${COMPOSE_PROJECT_NAME}_postgres_data" &>/dev/null; then
        warn "Es existiert bereits ein Datenbank-Volume „${COMPOSE_PROJECT_NAME}_postgres_data“,"
        warn "aber keine zugehörige .env mit den Zugangsdaten."
        echo ""
        echo "  Ein neues Passwort passt nicht zur bestehenden Datenbank – die Anwendung"
        echo "  käme mit „password authentication failed“ nicht hoch."
        echo ""
        echo "  Optionen:"
        echo "    1) Datenbank-Volume löschen und neu beginnen (ALLE DATEN GEHEN VERLOREN)"
        echo "    2) Abbrechen und die alte .env wiederherstellen"
        echo "    3) Anderen Projektnamen verwenden: COMPOSE_PROJECT_NAME=… ./install.sh"
        echo ""
        if [ "$UNATTENDED" = "true" ]; then
            error "Abbruch (unattended). Vorhandenes Volume: ${COMPOSE_PROJECT_NAME}_postgres_data"
        fi
        read -r -p "  Auswahl [2]: " volume_choice
        if [ "$volume_choice" = "1" ]; then
            # Erst die Version klären: Gibt es sie nicht, bleibt das Volume erhalten
            ensure_image_tag
            log "Entferne alte Volumes..."
            docker compose down -v --remove-orphans 2>/dev/null || true
            docker volume rm "${COMPOSE_PROJECT_NAME}_postgres_data" 2>/dev/null || true
        else
            log "Installation abgebrochen."
            exit 0
        fi
    fi

    # Bestehende Installation: Die .env enthält die Schlüssel zu Datenbank und verschlüsselten
    # Inhalten. Ohne Rückfrage wird sie nie ersetzt – aktualisiert wird mit ./update.sh.
    if [ -f ".env" ]; then
        warn "Bestehende Installation gefunden!"
        if [ "$UNATTENDED" = "true" ]; then
            if [ "$REINSTALL_DESTROY_DATA" != "true" ]; then
                error "Abbruch: In $SCRIPT_DIR liegt bereits eine Installation (.env vorhanden).
  Der unbeaufsichtigte Modus verändert eine bestehende Installation nicht.

  Aktualisieren:   ./update.sh
  Neu aufsetzen (löscht ALLE DATEN: Datenbank, Uploads, Dokument-Cache, Schlüssel):
                   ./install.sh --unattended --reinstall-destroy-data"
            fi
            warn "--reinstall-destroy-data: Die bestehende Installation wird samt aller Daten gelöscht."
        else
            echo ""
            echo "  Optionen:"
            echo "    1) Neu installieren (alle Daten werden gelöscht!)"
            echo "    2) Abbrechen (zum Updaten: ./update.sh)"
            echo ""
            read -p "  Auswahl [2]: " reinstall_choice
            if [[ ! "$reinstall_choice" =~ ^[1]$ ]]; then
                log "Installation abgebrochen."
                log "Zum Updaten: ./update.sh"
                exit 0
            fi
            # Erst die Version klären: Gibt es sie nicht, bleibt die bestehende Installation erhalten
            ensure_image_tag
        fi

        # Bisherige Konfiguration aufheben: Sie enthält den alten ENCRYPTION_MASTER_KEY, ohne
        # den ältere Sicherungen nicht mehr lesbar sind. (.env.* ist per .gitignore ausgeschlossen)
        local previous_env
        previous_env=".env.vor-neuinstallation-$(date +%Y%m%d_%H%M%S)"
        if ! (umask 077 && cp .env "$previous_env"); then
            error "Konnte die bisherige .env nicht nach $previous_env kopieren – Abbruch, nichts gelöscht."
        fi
        info "Bisherige Konfiguration aufgehoben: $previous_env (enthält die alten Schlüssel)"

        # Stop and remove existing containers + volumes (old passwords!)
        log "Stoppe bestehende Installation..."
        docker compose down -v 2>/dev/null || true
        log "Alte Container und Volumes entfernt"
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

    # Kontaktadresse für Let's Encrypt (optional; ohne Adresse holt Caddy die Zertifikate trotzdem)
    ACME_EMAIL=""
    if [ "$DOMAIN" != "localhost" ]; then
        while true; do
            read -p "E-Mail für Let's Encrypt (optional, leer = ohne): " input_email
            ACME_EMAIL="$input_email"
            if [ -n "$ACME_EMAIL" ] && is_placeholder_email "$ACME_EMAIL"; then
                warn "Platzhalteradressen lehnt Let's Encrypt ab. Bitte eine echte Adresse oder leer lassen."
                continue
            fi
            break
        done
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
        read -sp "Admin Passwort (min. ${ADMIN_PASSWORD_MIN_LENGTH} Zeichen): " ADMIN_PASSWORD
        echo ""
        if [ ${#ADMIN_PASSWORD} -lt "$ADMIN_PASSWORD_MIN_LENGTH" ]; then
            warn "Passwort muss mindestens ${ADMIN_PASSWORD_MIN_LENGTH} Zeichen lang sein."
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

        read -p "OParl sync interval on workdays 07-19 in minutes (nights hourly, weekends every 2 h) [15]: " input_sync_interval
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
# Kontaktadresse für Let's Encrypt; leer = ohne Adresse (keine Platzhalter, Let's Encrypt lehnt sie ab)
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
# Ohne Mailserver verschickt mandari keine Mails. Hier einkommentieren oder im Admin unter den
# Systemeinstellungen eintragen (dort hat der Eintrag Vorrang). Weitere Variablen: .env.example
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
    local spin_chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0

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
        printf "\b${spin_chars:$i:1}"
        i=$(( (i + 1) % ${#spin_chars} ))
        sleep 2
    done

    return 1
}

# =============================================================================
# Passphrase für die Konfiguration in der Sicherung
# =============================================================================
# backup.sh sichert die .env (mit ENCRYPTION_MASTER_KEY) nur verschlüsselt und liest die Passphrase
# vorgabemäßig aus ./.backup-passphrase. Ohne sie fehlte die Konfiguration in jeder nächtlichen
# Sicherung (Issue #962). Eine vorhandene Datei bleibt unverändert: Ältere Sicherungen brauchen sie.
setup_backup_passphrase() {
    if [ -f "$BACKUP_PASSPHRASE_DEFAULT_FILE" ]; then
        info "Passphrase für die Sicherung vorhanden: $SCRIPT_DIR/$BACKUP_PASSPHRASE_DEFAULT_FILE"
        return 0
    fi
    local passphrase
    passphrase="$(generate_password 40)"
    if [ "${#passphrase}" -lt 32 ] || ! (umask 077 && printf '%s\n' "$passphrase" > "$BACKUP_PASSPHRASE_DEFAULT_FILE"); then
        warn "Passphrase für die Sicherung nicht angelegt; die Konfiguration fehlt in den Sicherungen (DEPLOYMENT.md, „Sicherung“)."
        return 0
    fi
    chmod 600 "$BACKUP_PASSPHRASE_DEFAULT_FILE" 2>/dev/null || true
    log "Passphrase für die Sicherung angelegt: $SCRIPT_DIR/$BACKUP_PASSPHRASE_DEFAULT_FILE"
}

# =============================================================================
# Setup Automatic Daily Backup
# =============================================================================
setup_cron_backup() {
    setup_backup_passphrase
    if [ -x "./backup.sh" ]; then
        mkdir -p "$SCRIPT_DIR/logs"
        local cron_line="0 2 * * * cd $SCRIPT_DIR && ./backup.sh --quiet >> $SCRIPT_DIR/logs/backup.log 2>&1"
        # Ohne -q, damit grep die Eingabe zu Ende liest (pipefail, siehe defined_services in update.sh)
        if crontab -l 2>/dev/null | grep "mandari.*backup" > /dev/null; then
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
# Die Anwendung migriert beim Start nicht (docker-entrypoint.sh); Worker, Ingestor und
# Protokoll-Orchestrator brauchen aber das migrierte Schema. Deshalb:
#   1. Start infrastructure (postgres, redis, elasticsearch)
#   2. Start mandari (Django) and run migrations
#   3. Start the worker (events, tasks, schedules) – it needs the migrated schema
#   4. THEN start ingestor, minutes-orchestrator and caddy
# =============================================================================
start_services() {
    mkdir -p "$SCRIPT_DIR/logs"
    echo "" > "$INSTALL_LOG"

    # --- Pull ---
    run_step "Docker Images herunterladen" docker compose pull -q

    # --- Phase 1: Infrastructure ---
    log "Starte Infrastruktur..."
    docker compose up -d postgres redis elasticsearch >> "$INSTALL_LOG" 2>&1

    printf "  %-30s " "PostgreSQL"
    if wait_for_healthy "$DB_CONTAINER" 30; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    printf "  %-30s " "Redis"
    if wait_for_healthy "$REDIS_CONTAINER" 30; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    printf "  %-30s " "Elasticsearch"
    if wait_for_healthy "$SEARCH_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    # --- Phase 2: Mandari (Django) ---
    log "Starte Mandari..."
    docker compose up -d mandari >> "$INSTALL_LOG" 2>&1

    printf "  %-30s " "Mandari"
    if wait_for_healthy "$APP_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    run_migrations
    configure_oparl_sources

    # --- Phase 2b: Worker für Ereignisse, Aufträge und Zeitpläne (nach den Migrationen) ---
    # worker: alle Rollen ohne ocr/ai/live; worker-heavy: nur Aufträge aus ocr und ai; worker-live: nur live
    # (Live-Übertragungen, docs/LIVE_UEBERTRAGUNG.md; ohne Schalter wartet er nur), siehe docker-compose.yml
    log "Starte Worker..."
    docker compose up -d worker worker-heavy worker-live >> "$INSTALL_LOG" 2>&1

    printf "  %-30s " "Worker"
    if wait_for_healthy "$WORKER_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    printf "  %-30s " "Worker OCR/KI"
    if wait_for_healthy "$WORKER_HEAVY_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    printf "  %-30s " "Worker Live"
    if wait_for_healthy "$WORKER_LIVE_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    # --- Phase 3: Website (Wagtail) ---
    log "Starte Website..."
    docker compose up -d website >> "$INSTALL_LOG" 2>&1

    printf "  %-30s " "Website"
    if wait_for_healthy "$WEBSITE_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    run_website_migrations

    # --- Phase 4: Ingestor + Caddy ---
    log "Starte verbleibende Services..."
    docker compose up -d >> "$INSTALL_LOG" 2>&1

    printf "  %-30s " "Caddy (SSL)"
    if wait_for_healthy "$PROXY_CONTAINER" 60; then echo -e "${GREEN}✓${NC}"; else echo -e "${YELLOW}⏳${NC}"; fi

    log "Alle Services gestartet"

    setup_cron_backup
}

# =============================================================================
# Run Migrations
# =============================================================================
run_migrations() {
    # Wenn die Anwendung nicht gesund wurde, ist die Ursache fast immer die Datenbank-
    # verbindung. Dann hier abbrechen statt in einen Folgefehler zu laufen.
    if ! docker exec "$APP_CONTAINER" python -c "import django" &>/dev/null; then
        echo ""
        error "Die Anwendung ist nicht startbereit. Letzte Meldungen:
$(docker logs "$APP_CONTAINER" --tail 15 2>&1 | sed 's/^/    /')

  Häufigste Ursache: Das Datenbank-Volume stammt aus einer früheren Installation und
  das Passwort in .env passt nicht dazu. Vollständiges Log: $INSTALL_LOG"
    fi

    run_step "Datenbank-Migrationen" docker exec "$APP_CONTAINER" python manage.py migrate --noinput
    run_step "Rollen einrichten" docker exec "$APP_CONTAINER" python manage.py setup_roles || true
    run_step "Suchindex konfigurieren" docker exec "$APP_CONTAINER" python manage.py setup_elasticsearch
    create_superuser
}

# =============================================================================
# Create Superuser
# =============================================================================
create_superuser() {
    # Interaktiver Modus: nach Admin-Zugangsdaten fragen, wenn keine
    # Umgebungsvariablen gesetzt sind (Issue #16)
    if [ "$UNATTENDED" != "true" ] && { [ -z "${ADMIN_EMAIL:-}" ] || [ -z "${ADMIN_PASSWORD:-}" ]; }; then
        echo ""
        log "Admin-Account erstellen"
        echo "============================================"
        echo ""
        echo "  Ohne Admin-Account ist kein Login möglich (weder /admin noch Plattform)."
        echo "  Leer lassen zum Überspringen."
        echo ""
        read -p "  Admin E-Mail: " ADMIN_EMAIL
        if [ -n "$ADMIN_EMAIL" ]; then
            while true; do
                read -s -p "  Admin Passwort (min. ${ADMIN_PASSWORD_MIN_LENGTH} Zeichen): " ADMIN_PASSWORD; echo ""
                if [ "${#ADMIN_PASSWORD}" -lt "$ADMIN_PASSWORD_MIN_LENGTH" ]; then
                    echo "  Passwort zu kurz, bitte erneut."
                    continue
                fi
                read -s -p "  Passwort bestätigen: " ADMIN_PASSWORD2; echo ""
                if [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD2" ]; then
                    unset ADMIN_PASSWORD2
                    break
                fi
                echo "  Passwörter stimmen nicht überein, bitte erneut."
            done
        fi
    fi

    if [ -z "${ADMIN_EMAIL:-}" ] || [ -z "${ADMIN_PASSWORD:-}" ]; then
        info "Kein Admin-Account konfiguriert. Erstelle manuell:"
        info "  docker exec -it $APP_CONTAINER python manage.py createsuperuser"
        return
    fi

    # createsuperuser --noinput prüft die Passwortrichtlinie nicht: vorher mit den Prüfungen der Anwendung
    # (Länge, häufige Passwörter, Ähnlichkeit zur Adresse, nur Ziffern)
    if ! run_step "Admin-Passwort prüfen" docker exec \
        -e DJANGO_SUPERUSER_EMAIL="$ADMIN_EMAIL" \
        -e DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
        "$APP_CONTAINER" python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
validate_password(os.environ['DJANGO_SUPERUSER_PASSWORD'], get_user_model()(email=os.environ['DJANGO_SUPERUSER_EMAIL']))
"; then
        warn "Das Admin-Passwort erfüllt die Passwortrichtlinie nicht, kein Admin-Account angelegt:"
        warn "  $(grep -a 'ValidationError' "$INSTALL_LOG" | tail -n 1)"
        warn "  Anlegen mit: docker exec -it $APP_CONTAINER python manage.py createsuperuser"
        unset ADMIN_PASSWORD
        ADMIN_EMAIL=""
        return
    fi

    if ! run_step "Admin-Account erstellen" docker exec \
        -e DJANGO_SUPERUSER_EMAIL="$ADMIN_EMAIL" \
        -e DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
        "$APP_CONTAINER" python manage.py createsuperuser --noinput; then
        warn "Admin-Account nicht angelegt (etwa: Adresse schon vergeben). Details: $INSTALL_LOG"
        warn "  Anlegen mit: docker exec -it $APP_CONTAINER python manage.py createsuperuser"
        ADMIN_EMAIL=""
    fi

    # Passwort aus dem Speicher löschen
    unset ADMIN_PASSWORD
}

# =============================================================================
# Run Website Migrations
# =============================================================================
run_website_migrations() {
    run_step "Website-Migrationen" docker exec "$WEBSITE_CONTAINER" python manage.py migrate --noinput
}

# =============================================================================
# Configure OParl Sources (Interactive)
# =============================================================================
configure_oparl_sources() {
    if [ "$UNATTENDED" = "true" ]; then
        run_step "OParl-Quellen importieren" docker exec "$APP_CONTAINER" python manage.py import_oparl_sources
        info "OParl-Quellen importiert (inaktiv). Im Admin aktivieren."
        return
    fi

    echo ""
    log "OParl-Quellen konfigurieren"
    echo "============================================"
    echo ""
    echo "  Mandari synchronisiert Ratsinformationen über den OParl-Standard."
    echo "  Verfügbar: 111 Kommunen aus ganz Deutschland."
    echo ""
    echo -e "  ${CYAN}1)${NC} Alle Quellen aktivieren (111 Kommunen)"
    echo -e "  ${CYAN}2)${NC} Nur Großstädte (10 Kommunen)"
    echo -e "  ${CYAN}3)${NC} Einzelne Kommunen auswählen"
    echo -e "  ${CYAN}4)${NC} Überspringen (später im Admin aktivieren)"
    echo ""
    read -p "  Auswahl [4]: " oparl_choice

    # Import all sources (inactive by default)
    run_step "OParl-Quellen importieren" docker exec "$APP_CONTAINER" python manage.py import_oparl_sources

    case "${oparl_choice:-4}" in
        1)
            run_step "Alle Quellen aktivieren" docker exec "$APP_CONTAINER" python manage.py shell -c "
from insight_core.models import OParlSource
n = OParlSource.objects.all().update(is_active=True)
print(f'{n} Quellen aktiviert')
"
            ;;
        2)
            run_step "Großstädte aktivieren" docker exec "$APP_CONTAINER" python manage.py shell -c "
from insight_core.models import OParlSource
names = [
    'Stadt Köln', 'Landeshauptstadt Düsseldorf', 'Stadt Dresden',
    'Stadt Leipzig', 'Stadt Wuppertal', 'Stadt Münster',
    'Stadt Braunschweig', 'Stadt Krefeld', 'Stadt Freiburg',
    'München Transparent',
]
n = OParlSource.objects.filter(name__in=names).update(is_active=True)
print(f'{n} Großstadt-Quellen aktiviert')
"
            ;;
        3)
            select_individual_sources
            ;;
        4|"")
            info "OParl-Quellen können später im Admin aktiviert werden."
            info "  Admin: https://${DOMAIN}/admin/insight_core/oparlsource/"
            ;;
    esac
}

# =============================================================================
# Select Individual OParl Sources
# =============================================================================
select_individual_sources() {
    echo ""
    echo "  Verfügbare OParl-Quellen:"
    echo "  ─────────────────────────────────────────"

    # Display numbered list from database
    docker exec "$APP_CONTAINER" python manage.py shell -c "
from insight_core.models import OParlSource
for i, s in enumerate(OParlSource.objects.order_by('name'), 1):
    print(f'  {i:3d}) {s.name}')
"

    echo ""
    echo "  Eingabe: Nummern kommagetrennt, Bereiche oder einzeln"
    echo "  Beispiele: '1,3,5'  '1-10'  '42'  '1-5,8,12-15'"
    echo ""
    read -p "  Auswahl: " source_selection

    if [ -z "$source_selection" ]; then
        info "Keine Quellen ausgewählt."
        return
    fi

    echo ""
    # Activate selected sources via environment variable (safe from injection)
    docker exec -e SELECTION="$source_selection" "$APP_CONTAINER" python manage.py shell -c "
import os
from insight_core.models import OParlSource

selection = os.environ.get('SELECTION', '')
sources = list(OParlSource.objects.order_by('name'))
indices = set()

for part in selection.replace(' ', '').split(','):
    if '-' in part:
        try:
            start, end = part.split('-', 1)
            for i in range(int(start), int(end) + 1):
                indices.add(i)
        except ValueError:
            pass
    elif part.isdigit():
        indices.add(int(part))

activated = 0
for idx in sorted(indices):
    if 1 <= idx <= len(sources):
        sources[idx - 1].is_active = True
        sources[idx - 1].save(update_fields=['is_active', 'updated_at'])
        print(f'  ✓ {sources[idx - 1].name}')
        activated += 1

print(f'\n  {activated} Quelle(n) aktiviert.')
"
}

# =============================================================================
# Verify Installation
# =============================================================================
verify_installation() {
    log "Verifikation der Installation..."
    echo ""

    local all_ok=true

    # Check each container (Namen folgen dem Projektnamen, wie in backup.sh)
    local entry container label
    for entry in \
        "${DB_CONTAINER}:PostgreSQL" \
        "${REDIS_CONTAINER}:Redis" \
        "${SEARCH_CONTAINER}:Elasticsearch" \
        "${APP_CONTAINER}:Mandari" \
        "${WEBSITE_CONTAINER}:Website" \
        "${PROXY_CONTAINER}:Caddy" \
        "${COMPOSE_PROJECT_NAME}-ingestor:Ingestor" \
        "${WORKER_CONTAINER}:Worker" \
        "${WORKER_HEAVY_CONTAINER}:Worker OCR/KI" \
        "${WORKER_LIVE_CONTAINER}:Worker Live" \
        "${COMPOSE_PROJECT_NAME}-minutes-orchestrator:Orchestrator"; do
        container="${entry%%:*}"
        label="${entry#*:}"
        local status
        local health
        status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
        health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "no-healthcheck")

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
    if [ -f "$BACKUP_PASSPHRASE_DEFAULT_FILE" ]; then
        echo -e "  ${YELLOW}         Ebenso $BACKUP_PASSPHRASE_DEFAULT_FILE außerhalb des Servers verwahren:${NC}"
        echo -e "  ${YELLOW}         Ohne sie lässt sich die Konfiguration aus keiner Sicherung zurückholen.${NC}"
    fi
    echo ""
    echo "  Erste Schritte:"
    echo "    Anmeldung:       Beim ersten Login als Administrator richtet mandari die"
    echo "                     Zwei-Faktor-Anmeldung ein (Pflicht, docs/ACCOUNT_SECURITY.md)."
    echo "    Mailversand:     Ohne Mailserver gehen keine Mails raus (Einladungen, Passwort"
    echo "                     vergessen). Im Admin unter den Systemeinstellungen oder EMAIL_* in .env."
    echo "    Kommunen:        https://${DOMAIN}/admin/insight_core/oparlsource/ (einzeln prüfen und aktivieren)"
    echo ""
    info "Detaillierte Logs: cat $INSTALL_LOG"
    echo ""
}

# =============================================================================
# Main
# =============================================================================
show_usage() {
    cat << 'EOF'
Verwendung: ./install.sh [OPTIONEN]

Optionen:
  --tag TAG                  Image-Version für mandari, ingestor und website:
                               latest   neuester stabiler Stand (Vorgabe)
                               dev      Entwicklungsstand (Zweig dev, instabil)
                               v0.11.0  eine veröffentlichte Version, siehe
                                        https://github.com/mandariOSS/mandari/releases
                             Gibt es das Tag nicht für alle drei Images, bricht der Installer ab,
                             bevor er etwas verändert.
  --unattended               Ohne Rückfragen; Werte aus Umgebungsvariablen (DOMAIN, ACME_EMAIL,
                             ADMIN_EMAIL, ADMIN_PASSWORD, TZ, ...). Bricht mit Exit-Code 1 ab,
                             wenn in diesem Verzeichnis bereits eine Installation liegt oder
                             ADMIN_PASSWORD kürzer als 12 Zeichen ist. ACME_EMAIL ist optional.
  --reinstall-destroy-data   Nur mit --unattended: eine bestehende Installation verwerfen und
                             neu aufsetzen. LÖSCHT ALLE DATEN (Datenbank, Uploads, Dokument-Cache,
                             Suchindex, Zertifikate) und erzeugt neue Schlüssel. Interaktiv
                             fragt der Installer stattdessen nach.
  -h, --help                 Diese Hilfe anzeigen

Eine bestehende Installation aktualisieren: ./update.sh
EOF
}

main() {
    UNATTENDED=false
    REINSTALL_DESTROY_DATA=false
    IMAGE_TAG=""
    IMAGE_TAG_GEPRUEFT=""

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --unattended)
                UNATTENDED=true
                shift
                ;;
            --reinstall-destroy-data)
                REINSTALL_DESTROY_DATA=true
                shift
                ;;
            -h|--help)
                show_usage
                exit 0
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
                show_usage
                error "Unbekanntes Argument: $1"
                ;;
        esac
    done

    # Unbeaufsichtigt steht die Version vor der Prüfung der Voraussetzungen fest (dort wird sie geprüft)
    if [ "$UNATTENDED" = "true" ]; then
        IMAGE_TAG="${IMAGE_TAG:-latest}"
        # Vor jeder Änderung (auch vor --reinstall-destroy-data): ein zu kurzes Passwort legte sonst
        # später keinen Admin-Account an
        if [ -n "${ADMIN_PASSWORD:-}" ] && [ "${#ADMIN_PASSWORD}" -lt "$ADMIN_PASSWORD_MIN_LENGTH" ]; then
            error "ADMIN_PASSWORD ist kürzer als ${ADMIN_PASSWORD_MIN_LENGTH} Zeichen (Passwortrichtlinie). Es wurde nichts verändert."
        fi
    fi

    show_banner
    check_prerequisites

    # Check for unattended mode
    if [ "$UNATTENDED" = "true" ]; then
        DOMAIN="${DOMAIN:-localhost}"
        ACME_EMAIL="${ACME_EMAIL:-}"
        accept_acme_email
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
    fi

    # Kanalwahl (interaktiv ohne --tag), sofern nicht schon vor einer Neuinstallation geschehen
    ensure_image_tag

    log "Image-Tag: $IMAGE_TAG"
    generate_secrets
    create_env_file
    start_services
    show_summary
}

# Run main function
main "$@"
