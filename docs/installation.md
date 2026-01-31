# Installation Guide

This guide covers installing Mandari Community Edition on a single server.

## Requirements

### Hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB |
| Storage | 20 GB SSD | 50 GB SSD |

### Software

- **Operating System**: Ubuntu 22.04 LTS (recommended), Debian 12, or other Linux distribution
- **Docker**: Version 24.0 or higher
- **Docker Compose**: Version 2.20 or higher (included with Docker Desktop)

### Network

- Port 80 (HTTP) - required for SSL certificate verification
- Port 443 (HTTPS) - for secure access
- A domain name pointing to your server (for production)

## Quick Start

### Option 1: Interactive Installer (Recommended)

```bash
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./install.sh
```

The installer will:
1. Check prerequisites
2. Ask for configuration (domain, email, etc.)
3. Generate secure passwords and keys
4. Start all services
5. Run database migrations

### Option 2: Manual Installation

```bash
# Clone repository
git clone https://github.com/mandariOSS/mandari.git
cd mandari

# Copy and edit configuration
cp .env.example .env
nano .env  # Edit with your values

# Generate secure keys (if not using installer)
echo "SECRET_KEY=$(openssl rand -base64 50)"
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 32)"
echo "MEILISEARCH_KEY=$(openssl rand -base64 32)"
echo "ENCRYPTION_MASTER_KEY=$(openssl rand -base64 32)"

# Start services
docker compose up -d

# Run migrations
docker exec mandari-api python manage.py migrate
docker exec mandari-api python manage.py setup_roles
```

## Docker Installation

### Ubuntu/Debian

```bash
# Update packages
sudo apt update

# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add your user to docker group (logout/login required)
sudo usermod -aG docker $USER

# Verify installation
docker --version
docker compose version
```

### Other Distributions

See the official Docker documentation:
- [Install Docker Engine](https://docs.docker.com/engine/install/)
- [Install Docker Compose](https://docs.docker.com/compose/install/)

## SSL Certificates

Mandari uses Caddy with automatic SSL via Let's Encrypt.

### Requirements for HTTPS

1. A valid domain name (e.g., `mandari.example.com`)
2. DNS A record pointing to your server's IP
3. Ports 80 and 443 accessible from the internet

### How it works

1. When you first access `https://your-domain.com`, Caddy automatically:
   - Requests a certificate from Let's Encrypt
   - Validates domain ownership via HTTP-01 challenge
   - Configures HTTPS

2. Certificates are automatically renewed before expiration

### Troubleshooting SSL

```bash
# Check Caddy logs
docker logs mandari-caddy

# Verify DNS
dig your-domain.com

# Check ports are open
curl -I http://your-domain.com/.well-known/acme-challenge/test
```

## Post-Installation

### Create Admin User

```bash
docker exec -it mandari-api python manage.py createsuperuser
```

### Access the Admin Panel

Open `https://your-domain.com/admin` in your browser.

### Add OParl Sources

1. Go to Admin > Insight Core > OParl Sources
2. Add your RIS endpoints (e.g., `https://oparl.stadt-muenster.de/system`)
3. The ingestor will automatically sync data

### Check Service Status

```bash
# All services
docker compose ps

# API logs
docker compose logs -f api

# Ingestor logs
docker compose logs -f ingestor
```

## Firewall Configuration

### UFW (Ubuntu)

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### iptables

```bash
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
```

## Uninstallation

```bash
# Stop and remove containers
docker compose down

# Remove volumes (WARNING: deletes all data!)
docker compose down -v

# Remove images
docker rmi $(docker images -q ghcr.io/mandarioss/*)
```

## Next Steps

- [Configuration Guide](configuration.md) - Customize your installation
- [Backup & Restore](backup-restore.md) - Protect your data
- [Upgrading](upgrading.md) - Update to new versions
