# Mandari 2.0 - Infrastructure

This directory contains the Infrastructure as Code (IaC) for deploying Mandari 2.0 to production.

## Architecture

```
                         ┌─────────────────────────────────┐
                         │    Hetzner Load Balancer        │
                         │    (mandari.de :80/:443)        │
                         └─────────────┬───────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  │
    ┌───────────────────────┐  ┌───────────────────────┐ │
    │      VM 1 (Master)    │  │      VM 2 (Slave)     │ │
    │                       │  │                       │ │
    │  - Caddy (Proxy)      │  │  - Caddy (Proxy)      │ │
    │  - API                │  │  - API                │ │
    │  - Web Frontends      │  │  - Web Frontends      │ │
    │  - Ingestor (✓)       │  │  - (no ingestor)      │ │
    │  - PostgreSQL PRIMARY │  │  - PostgreSQL REPLICA │ │
    │  - Redis MASTER       │  │  - Redis REPLICA      │ │
    │  - Meilisearch        │  │  - Meilisearch        │ │
    └───────────────────────┘  └───────────────────────┘
```

## Directory Structure

```
infrastructure/
├── terraform/           # Hetzner Cloud provisioning
│   ├── main.tf          # Main infrastructure definition
│   ├── variables.tf     # Input variables
│   ├── outputs.tf       # Output values
│   └── terraform.tfvars.example
│
├── ansible/             # Server configuration
│   ├── ansible.cfg      # Ansible configuration
│   ├── inventory/       # Server inventory
│   └── playbooks/       # Configuration playbooks
│       ├── setup.yml    # Initial server setup
│       ├── deploy.yml   # Application deployment
│       ├── postgres-setup.yml # Database replication
│       └── rollback.yml # Rollback procedure
│
├── docker/              # Container configuration
│   ├── docker-compose.master.yml
│   ├── docker-compose.slave.yml
│   ├── Caddyfile
│   └── .env.example
│
├── scripts/             # Deployment automation
│   ├── deploy.sh        # Main deployment script
│   ├── failover.sh      # Database failover
│   └── backup.sh        # Backup management
│
└── README.md            # This file
```

## Prerequisites

1. **Hetzner Cloud Account** with API token
2. **Terraform** >= 1.0
3. **Ansible** >= 2.14
4. **SSH Key** (ed25519 recommended)

### Installation (macOS/Linux)

```bash
# Terraform
brew install terraform  # or download from terraform.io

# Ansible
pip3 install ansible ansible-lint

# Hetzner CLI (optional but useful)
brew install hcloud
```

## Quick Start

### 1. Configure Terraform

```bash
cd terraform

# Copy and edit variables
cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars  # Add your Hetzner API token
```

### 2. Configure Environment

```bash
cd ../docker

# Copy and edit environment
cp .env.example .env
vim .env  # Set passwords and secrets
```

### 3. Deploy

```bash
cd ../scripts

# Full deployment (infrastructure + setup + app)
./deploy.sh all

# Or step by step:
./deploy.sh infra   # Provision Hetzner resources
./deploy.sh setup   # Configure servers
./deploy.sh app     # Deploy application
```

### 4. Verify

```bash
# Check deployment status
./deploy.sh status

# Check PostgreSQL replication
./failover.sh check
```

## Deployment Commands

| Command | Description |
|---------|-------------|
| `./deploy.sh infra` | Provision Hetzner infrastructure |
| `./deploy.sh setup` | Configure servers (Docker, security, etc.) |
| `./deploy.sh app` | Deploy/update application |
| `./deploy.sh all` | Run all steps |
| `./deploy.sh status` | Check deployment status |

## Backup Commands

| Command | Description |
|---------|-------------|
| `./backup.sh db` | Backup PostgreSQL database |
| `./backup.sh files` | Backup uploaded files |
| `./backup.sh full` | Full backup (db + files) |
| `./backup.sh list` | List available backups |
| `./backup.sh restore-db <file>` | Restore database |

## Failover Commands

| Command | Description |
|---------|-------------|
| `./failover.sh check` | Check replication status |
| `./failover.sh promote` | Emergency failover (promote slave) |
| `./failover.sh switchover` | Controlled switchover |

## Estimated Costs (Hetzner)

| Resource | Type | Cost/Month |
|----------|------|------------|
| 2x VM | cx31 (2 vCPU, 8GB) | €31.18 |
| 1x Load Balancer | lb11 | €5.39 |
| 2x Volume | 50GB | €4.80 |
| **Total** | | **~€42/month** |

## Security Considerations

- All sensitive data is encrypted at rest (AES-256-GCM)
- PostgreSQL connections require SSL
- UFW firewall restricts access
- fail2ban protects against brute force
- SSH key authentication only (no passwords)
- Non-root containers

## Monitoring

Basic monitoring is available through:
- Hetzner Cloud Console (CPU, network, disk)
- Docker health checks
- `/health` endpoints

For advanced monitoring, consider adding:
- Prometheus + Grafana
- Loki for log aggregation
- Sentry for error tracking

## Troubleshooting

### Servers Not Accessible

```bash
# Check if servers are running
hcloud server list

# Check firewall rules
hcloud firewall describe mandari-firewall
```

### PostgreSQL Replication Issues

```bash
# On master: Check replication slots
docker exec mandari-postgres psql -U mandari -c "SELECT * FROM pg_replication_slots;"

# On slave: Check recovery status
docker exec mandari-postgres psql -U mandari -c "SELECT pg_is_in_recovery();"
```

### Containers Not Starting

```bash
# Check logs
docker compose -f /opt/mandari/docker-compose.yml logs -f

# Check specific service
docker logs mandari-api
```

## Contributing

1. Test changes in a staging environment first
2. Update documentation when adding features
3. Use meaningful commit messages
