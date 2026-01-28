#!/bin/bash
# =============================================================================
# Mandari 2.0 - Deployment Script
# =============================================================================
# This script orchestrates the deployment process for Mandari 2.0
#
# Usage:
#   ./deploy.sh infra    - Provision Hetzner infrastructure with Terraform
#   ./deploy.sh setup    - Configure servers with Ansible
#   ./deploy.sh app      - Deploy application
#   ./deploy.sh all      - Run all steps
#   ./deploy.sh status   - Check deployment status
# =============================================================================

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log() { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"; }
error() { echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"; }

# Check prerequisites
check_prerequisites() {
    log "Checking prerequisites..."

    # Check Terraform
    if ! command -v terraform &> /dev/null; then
        error "Terraform is not installed. Please install it first."
    fi

    # Check Ansible
    if ! command -v ansible-playbook &> /dev/null; then
        error "Ansible is not installed. Please install it first."
    fi

    # Check SSH key
    if [ ! -f "$HOME/.ssh/id_ed25519.pub" ]; then
        warn "SSH key not found at ~/.ssh/id_ed25519.pub"
        warn "You may need to specify a different key path."
    fi

    log "All prerequisites met."
}

# Load environment variables
load_env() {
    if [ -f "$PROJECT_ROOT/.env.prod" ]; then
        log "Loading environment from .env.prod..."
        set -a
        source "$PROJECT_ROOT/.env.prod"
        set +a
    elif [ -f "$PROJECT_ROOT/docker/.env" ]; then
        log "Loading environment from docker/.env..."
        set -a
        source "$PROJECT_ROOT/docker/.env"
        set +a
    else
        warn "No environment file found. Make sure environment variables are set."
    fi
}

# =============================================================================
# Terraform Functions
# =============================================================================

deploy_infrastructure() {
    log "Provisioning Hetzner infrastructure with Terraform..."
    cd "$PROJECT_ROOT/terraform"

    # Check for tfvars file
    if [ ! -f "terraform.tfvars" ]; then
        error "terraform.tfvars not found. Copy terraform.tfvars.example and fill in your values."
    fi

    # Initialize Terraform
    log "Initializing Terraform..."
    terraform init -upgrade

    # Plan
    log "Planning infrastructure changes..."
    terraform plan -out=tfplan

    # Ask for confirmation
    read -p "Do you want to apply these changes? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        log "Deployment cancelled."
        exit 0
    fi

    # Apply
    log "Applying infrastructure changes..."
    terraform apply tfplan

    # Export outputs
    log "Exporting Terraform outputs..."
    export MASTER_IP=$(terraform output -raw master_ip)
    export SLAVE_IP=$(terraform output -raw slave_ip)
    export LB_IP=$(terraform output -raw lb_ip)

    log "Infrastructure provisioned successfully!"
    info "Master IP: $MASTER_IP"
    info "Slave IP: $SLAVE_IP"
    info "Load Balancer IP: $LB_IP"

    # Save to file for other scripts
    cat > "$PROJECT_ROOT/.infra_outputs" <<EOF
MASTER_IP=$MASTER_IP
SLAVE_IP=$SLAVE_IP
LB_IP=$LB_IP
EOF

    log "Outputs saved to $PROJECT_ROOT/.infra_outputs"
}

# =============================================================================
# Ansible Functions
# =============================================================================

load_infra_outputs() {
    if [ -f "$PROJECT_ROOT/.infra_outputs" ]; then
        source "$PROJECT_ROOT/.infra_outputs"
    else
        # Try to get from Terraform
        cd "$PROJECT_ROOT/terraform"
        if terraform output master_ip &> /dev/null; then
            export MASTER_IP=$(terraform output -raw master_ip)
            export SLAVE_IP=$(terraform output -raw slave_ip)
            export LB_IP=$(terraform output -raw lb_ip)
        else
            error "No infrastructure outputs found. Run './deploy.sh infra' first."
        fi
    fi
}

setup_servers() {
    log "Configuring servers with Ansible..."
    load_infra_outputs
    load_env

    cd "$PROJECT_ROOT/ansible"

    # Wait for servers to be ready
    log "Waiting for servers to be accessible..."
    for ip in "$MASTER_IP" "$SLAVE_IP"; do
        until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@$ip echo "Server $ip ready" 2>/dev/null; do
            info "Waiting for $ip..."
            sleep 10
        done
    done

    # Run initial setup
    log "Running initial server setup..."
    ansible-playbook -i inventory/production.yml playbooks/setup.yml

    # Setup PostgreSQL replication
    log "Setting up PostgreSQL replication..."
    ansible-playbook -i inventory/production.yml playbooks/postgres-setup.yml

    log "Server setup complete!"
}

deploy_app() {
    log "Deploying application..."
    load_infra_outputs
    load_env

    cd "$PROJECT_ROOT/ansible"

    # Deploy application
    log "Running deployment playbook..."
    ansible-playbook -i inventory/production.yml playbooks/deploy.yml

    log "Application deployed successfully!"
    info "Access your application at: https://mandari.de"
    info "(Make sure DNS is pointed to: $LB_IP)"
}

# =============================================================================
# Status Functions
# =============================================================================

check_status() {
    log "Checking deployment status..."
    load_infra_outputs

    echo ""
    echo "=========================================="
    echo "         MANDARI DEPLOYMENT STATUS"
    echo "=========================================="
    echo ""

    # Check Load Balancer
    info "Load Balancer: $LB_IP"
    if curl -s -o /dev/null -w "%{http_code}" "http://$LB_IP/health" | grep -q "200"; then
        echo -e "  Status: ${GREEN}Healthy${NC}"
    else
        echo -e "  Status: ${RED}Unhealthy${NC}"
    fi
    echo ""

    # Check Master
    info "Master Server: $MASTER_IP"
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@$MASTER_IP "docker ps --format '{{.Names}}: {{.Status}}'" 2>/dev/null; then
        echo ""
    else
        echo -e "  Status: ${RED}Unreachable${NC}"
    fi
    echo ""

    # Check Slave
    info "Slave Server: $SLAVE_IP"
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@$SLAVE_IP "docker ps --format '{{.Names}}: {{.Status}}'" 2>/dev/null; then
        echo ""
    else
        echo -e "  Status: ${RED}Unreachable${NC}"
    fi
    echo ""

    # Check PostgreSQL Replication
    info "PostgreSQL Replication:"
    ssh -o StrictHostKeyChecking=no root@$MASTER_IP "docker exec mandari-postgres psql -U mandari -t -c \"SELECT client_addr, state, pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) as lag FROM pg_stat_replication;\"" 2>/dev/null || echo "  Unable to check replication status"
    echo ""

    echo "=========================================="
}

# =============================================================================
# Help
# =============================================================================

show_help() {
    echo "Mandari 2.0 Deployment Script"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  infra    Provision Hetzner infrastructure with Terraform"
    echo "  setup    Configure servers with Ansible (initial setup)"
    echo "  app      Deploy/update the application"
    echo "  all      Run all steps (infra → setup → app)"
    echo "  status   Check deployment status"
    echo "  help     Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 all      # Full deployment"
    echo "  $0 app      # Deploy only application (servers already set up)"
    echo "  $0 status   # Check if everything is running"
}

# =============================================================================
# Main
# =============================================================================

main() {
    check_prerequisites

    case "${1:-help}" in
        infra)
            deploy_infrastructure
            ;;
        setup)
            setup_servers
            ;;
        app)
            deploy_app
            ;;
        all)
            deploy_infrastructure
            setup_servers
            deploy_app
            check_status
            ;;
        status)
            check_status
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
}

main "$@"
