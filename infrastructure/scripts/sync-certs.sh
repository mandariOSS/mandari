#!/bin/bash
# =============================================================================
# Mandari 2.0 - Certificate Sync Script
# =============================================================================
# Syncs SSL certificates from Master to Slave server.
# Run this on the SLAVE server via cron (e.g., daily).
#
# The Master server is the authoritative source for certificates.
# Caddy on Master handles Let's Encrypt renewal via DNS-01 challenge.
# This script copies the renewed certificates to the Slave.
#
# Usage:
#   ./sync-certs.sh              - Sync certificates from Master
#   ./sync-certs.sh --check      - Check certificate expiry dates
#
# Cron example (run daily at 3 AM):
#   0 3 * * * /opt/mandari/scripts/sync-certs.sh >> /var/log/cert-sync.log 2>&1
# =============================================================================

set -euo pipefail

# Configuration
MASTER_IP="${MASTER_PRIVATE_IP:-10.0.0.3}"
APP_DIR="${APP_DIR:-/opt/mandari}"
CADDY_VOLUME="mandari_caddy_data"
LOG_PREFIX="[cert-sync]"

# Colors (for interactive use)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') $1"; }
warn() { echo -e "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}WARNING:${NC} $1"; }
error() { echo -e "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') ${RED}ERROR:${NC} $1"; exit 1; }
success() { echo -e "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') ${GREEN}SUCCESS:${NC} $1"; }

# =============================================================================
# Check Mode - Show certificate expiry
# =============================================================================
check_certs() {
    log "Checking certificate status..."

    # Check local certificate
    echo ""
    echo "=== Local Certificate (Slave) ==="
    docker run --rm -v ${CADDY_VOLUME}:/data alpine:latest \
        sh -c "cat /data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/*/mandari.de.crt 2>/dev/null" | \
        openssl x509 -noout -dates -subject 2>/dev/null || echo "No local certificate found"

    # Check Master certificate (via SSH)
    echo ""
    echo "=== Master Certificate ==="
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@${MASTER_IP} \
        "docker run --rm -v mandari_caddy_data:/data alpine:latest \
         sh -c \"cat /data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/*/mandari.de.crt 2>/dev/null\"" 2>/dev/null | \
        openssl x509 -noout -dates -subject 2>/dev/null || echo "Cannot reach Master or no certificate"

    echo ""
}

# =============================================================================
# Sync Certificates from Master
# =============================================================================
sync_certs() {
    log "Starting certificate sync from Master ($MASTER_IP)..."

    # Check if Master is reachable
    if ! ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@${MASTER_IP} "echo ok" &>/dev/null; then
        error "Cannot reach Master server at $MASTER_IP"
    fi

    # Create temp directory
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    # Export certificates from Master
    log "Exporting certificates from Master..."
    ssh -o StrictHostKeyChecking=no root@${MASTER_IP} << 'REMOTE_EOF'
        TEMP_EXPORT=$(mktemp -d)
        docker run --rm \
            -v mandari_caddy_data:/data:ro \
            -v ${TEMP_EXPORT}:/backup \
            alpine:latest \
            sh -c "cp -r /data/caddy /backup/ 2>/dev/null || true"

        # Create archive
        tar -czf /tmp/caddy-certs.tar.gz -C ${TEMP_EXPORT} .
        rm -rf ${TEMP_EXPORT}
        echo "/tmp/caddy-certs.tar.gz"
REMOTE_EOF

    # Download archive
    log "Downloading certificates..."
    scp -o StrictHostKeyChecking=no root@${MASTER_IP}:/tmp/caddy-certs.tar.gz ${TEMP_DIR}/

    # Clean up on Master
    ssh -o StrictHostKeyChecking=no root@${MASTER_IP} "rm -f /tmp/caddy-certs.tar.gz"

    # Check if we got certificates
    if [ ! -f "${TEMP_DIR}/caddy-certs.tar.gz" ]; then
        error "Failed to download certificates from Master"
    fi

    # Extract and check contents
    tar -tzf ${TEMP_DIR}/caddy-certs.tar.gz | grep -q "caddy" || error "Invalid certificate archive"

    # Import into local Caddy volume
    log "Importing certificates into local Caddy volume..."

    # Extract to temp location
    mkdir -p ${TEMP_DIR}/extract
    tar -xzf ${TEMP_DIR}/caddy-certs.tar.gz -C ${TEMP_DIR}/extract

    # Copy into Docker volume
    docker run --rm \
        -v ${CADDY_VOLUME}:/data \
        -v ${TEMP_DIR}/extract:/backup:ro \
        alpine:latest \
        sh -c "rm -rf /data/caddy && cp -r /backup/caddy /data/"

    # Reload Caddy to pick up new certificates
    log "Reloading Caddy..."
    docker exec mandari-caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || \
        warn "Could not reload Caddy (may need container restart)"

    success "Certificate sync completed!"

    # Show new certificate info
    log "New certificate info:"
    docker run --rm -v ${CADDY_VOLUME}:/data alpine:latest \
        sh -c "cat /data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/*/mandari.de.crt 2>/dev/null" | \
        openssl x509 -noout -dates 2>/dev/null || true
}

# =============================================================================
# Main
# =============================================================================
case "${1:-sync}" in
    --check|check)
        check_certs
        ;;
    --sync|sync|"")
        sync_certs
        ;;
    --help|-h)
        echo "Usage: $0 [--check|--sync]"
        echo ""
        echo "Commands:"
        echo "  --sync   Sync certificates from Master (default)"
        echo "  --check  Check certificate expiry dates"
        ;;
    *)
        error "Unknown command: $1"
        ;;
esac
