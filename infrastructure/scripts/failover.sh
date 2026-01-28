#!/bin/bash
# =============================================================================
# Mandari 2.0 - Manual Failover Script
# =============================================================================
# This script performs a manual failover from master to slave.
# USE WITH CAUTION - This will promote the replica to become the new primary.
#
# Usage:
#   ./failover.sh check     - Check replication status
#   ./failover.sh promote   - Promote slave to master (DANGEROUS)
#   ./failover.sh switchover - Controlled switchover (safer)
# =============================================================================

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Logging
log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"; }
error() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"; exit 1; }

# Load infrastructure outputs
load_config() {
    if [ -f "$PROJECT_ROOT/.infra_outputs" ]; then
        source "$PROJECT_ROOT/.infra_outputs"
    else
        error "Infrastructure outputs not found. Run deploy.sh first."
    fi
}

# =============================================================================
# Check Functions
# =============================================================================

check_replication() {
    log "Checking PostgreSQL replication status..."
    load_config

    echo ""
    echo "=========================================="
    echo "      PostgreSQL Replication Status"
    echo "=========================================="
    echo ""

    # Master status
    echo -e "${BLUE}=== Master ($MASTER_IP) ===${NC}"
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP << 'EOF'
docker exec mandari-postgres psql -U mandari -c "
SELECT
    pid,
    usename,
    client_addr,
    state,
    sent_lsn,
    write_lsn,
    flush_lsn,
    replay_lsn,
    pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) as replication_lag,
    sync_state
FROM pg_stat_replication;
"

echo ""
echo "Replication Slots:"
docker exec mandari-postgres psql -U mandari -c "
SELECT
    slot_name,
    slot_type,
    active,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as slot_lag
FROM pg_replication_slots;
"
EOF

    echo ""
    echo -e "${BLUE}=== Slave ($SLAVE_IP) ===${NC}"
    ssh -o StrictHostKeyChecking=no root@$SLAVE_IP << 'EOF'
docker exec mandari-postgres psql -U mandari -c "
SELECT
    pg_is_in_recovery() as is_replica,
    pg_last_wal_receive_lsn() as received_lsn,
    pg_last_wal_replay_lsn() as replayed_lsn,
    pg_last_xact_replay_timestamp() as last_replay_time,
    now() - pg_last_xact_replay_timestamp() as replication_delay;
"
EOF

    echo ""
    echo "=========================================="
}

# =============================================================================
# Promote Functions
# =============================================================================

promote_slave() {
    warn "=== DANGER: PostgreSQL Failover ==="
    warn "This will:"
    warn "  1. Stop writes on the master"
    warn "  2. Promote the slave to become the new primary"
    warn "  3. Update load balancer to point only to slave"
    warn ""
    warn "The old master will need manual intervention to become a replica."
    warn ""

    read -p "Are you ABSOLUTELY sure? Type 'PROMOTE' to continue: " confirm
    if [ "$confirm" != "PROMOTE" ]; then
        log "Failover cancelled."
        exit 0
    fi

    load_config

    # Step 1: Check slave is caught up
    log "Checking replication lag..."
    lag=$(ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "docker exec mandari-postgres psql -U mandari -t -c \"SELECT pg_wal_lsn_diff(sent_lsn, replay_lsn) FROM pg_stat_replication;\"" | tr -d ' ')

    if [ "$lag" -gt 0 ]; then
        warn "Replication lag detected: $lag bytes"
        read -p "Continue anyway? (yes/no): " cont
        if [ "$cont" != "yes" ]; then
            error "Failover aborted due to replication lag"
        fi
    fi

    # Step 2: Stop the master
    log "Stopping master PostgreSQL..."
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "docker compose -f /opt/mandari/docker-compose.yml stop postgres"

    # Step 3: Promote slave
    log "Promoting slave to primary..."
    ssh -o StrictHostKeyChecking=no root@$SLAVE_IP << 'EOF'
# Remove standby signal
docker exec mandari-postgres rm -f /var/lib/postgresql/data/standby.signal

# Promote
docker exec mandari-postgres pg_ctl promote -D /var/lib/postgresql/data

# Verify
sleep 5
docker exec mandari-postgres psql -U mandari -c "SELECT pg_is_in_recovery();"
EOF

    # Step 4: Update DNS/LB (manual step notification)
    log "Slave promoted to primary!"
    warn ""
    warn "=== MANUAL STEPS REQUIRED ==="
    warn "1. Update DNS to point to slave IP: $SLAVE_IP"
    warn "   OR update Hetzner Load Balancer to remove master target"
    warn ""
    warn "2. Update the slave's docker-compose.yml:"
    warn "   - Change from docker-compose.slave.yml to docker-compose.master.yml"
    warn "   - Enable the ingestor service"
    warn ""
    warn "3. To make the old master a replica, run:"
    warn "   ssh root@$MASTER_IP '/opt/mandari/scripts/init-replica.sh'"
    warn ""
    warn "=== END MANUAL STEPS ==="

    log "Failover complete (pending manual steps)"
}

# =============================================================================
# Controlled Switchover
# =============================================================================

controlled_switchover() {
    log "=== Controlled Switchover ==="
    log "This performs a safer switchover by:"
    log "  1. Waiting for replication to fully sync"
    log "  2. Stopping writes gracefully"
    log "  3. Promoting the replica"
    log "  4. Converting old primary to replica"
    log ""

    read -p "Continue with controlled switchover? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        log "Switchover cancelled."
        exit 0
    fi

    load_config

    # Step 1: Enable synchronous replication temporarily
    log "Enabling synchronous replication for safe switchover..."
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP << 'EOF'
docker exec mandari-postgres psql -U mandari -c "ALTER SYSTEM SET synchronous_standby_names = 'replica';"
docker exec mandari-postgres psql -U mandari -c "SELECT pg_reload_conf();"
EOF

    # Wait for sync
    log "Waiting for synchronous replication to establish..."
    sleep 10

    # Step 2: Stop application writes
    log "Stopping API container to prevent new writes..."
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "docker compose -f /opt/mandari/docker-compose.yml stop api ingestor"

    # Step 3: Wait for final sync
    log "Waiting for final WAL sync..."
    sleep 5

    # Verify sync
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP << 'EOF'
echo "Final replication status:"
docker exec mandari-postgres psql -U mandari -c "
SELECT sync_state, pg_wal_lsn_diff(sent_lsn, replay_lsn) as lag
FROM pg_stat_replication;
"
EOF

    # Step 4: Stop master PostgreSQL
    log "Stopping master PostgreSQL..."
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "docker compose -f /opt/mandari/docker-compose.yml stop postgres"

    # Step 5: Promote slave
    log "Promoting slave..."
    ssh -o StrictHostKeyChecking=no root@$SLAVE_IP << 'EOF'
docker exec mandari-postgres rm -f /var/lib/postgresql/data/standby.signal
docker exec mandari-postgres pg_ctl promote -D /var/lib/postgresql/data
sleep 5
docker exec mandari-postgres psql -U mandari -c "SELECT pg_is_in_recovery();"
EOF

    # Step 6: Convert old master to replica
    log "Converting old master to replica..."
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP << EOF
# Clear data
rm -rf /mnt/data/postgres/*

# Run basebackup from new primary
docker run --rm \
    -v /mnt/data/postgres:/var/lib/postgresql/data \
    -e PGPASSWORD=\${REPLICATION_PASSWORD} \
    --network host \
    postgres:16-alpine \
    pg_basebackup \
        -h $SLAVE_IP \
        -p 5432 \
        -U replicator \
        -D /var/lib/postgresql/data \
        -Fp -Xs -P -R

# Copy replica config
cp /opt/mandari/postgres/standby.signal /mnt/data/postgres/
chown -R 999:999 /mnt/data/postgres
EOF

    # Step 7: Swap compose files
    log "Swapping docker-compose configurations..."
    ssh -o StrictHostKeyChecking=no root@$SLAVE_IP \
        "ln -sf /opt/mandari/docker-compose.master.yml /opt/mandari/docker-compose.yml"

    ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "ln -sf /opt/mandari/docker-compose.slave.yml /opt/mandari/docker-compose.yml"

    # Step 8: Start everything
    log "Starting all services..."
    ssh -o StrictHostKeyChecking=no root@$SLAVE_IP \
        "docker compose -f /opt/mandari/docker-compose.yml up -d"

    ssh -o StrictHostKeyChecking=no root@$MASTER_IP \
        "docker compose -f /opt/mandari/docker-compose.yml up -d"

    log "Controlled switchover complete!"
    log "New primary: $SLAVE_IP"
    log "New replica: $MASTER_IP"
    warn ""
    warn "Remember to update any hardcoded references to master/slave IPs!"
}

# =============================================================================
# Help
# =============================================================================

show_help() {
    echo "Mandari 2.0 - Failover Script"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  check      Check replication status"
    echo "  promote    Emergency failover - promote slave to master"
    echo "  switchover Controlled switchover (safer, swaps roles)"
    echo ""
    echo "WARNING: Failover operations are dangerous and may cause data loss."
    echo "Always verify replication status before proceeding."
}

# =============================================================================
# Main
# =============================================================================

case "${1:-help}" in
    check)
        check_replication
        ;;
    promote)
        promote_slave
        ;;
    switchover)
        controlled_switchover
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
