# Mandari Server Setup Guide

## Voraussetzungen

- **2x Hetzner VMs** (Ubuntu 24.04 empfohlen)
- **1x Hetzner Load Balancer** (zeigt auf beide VMs)
- **SSH-Zugang** zu beiden Servern (als root)
- **Domain** (z.B. mandari.de) zeigt auf Load Balancer IP

## Quick Start

### 1. Setup-Script ausführen

```bash
cd infrastructure/scripts

# Server IPs setzen
export MASTER_IP="your-master-ip"
export SLAVE_IP="your-slave-ip"
export DOMAIN="mandari.de"

# Script ausführen
chmod +x setup-servers.sh
./setup-servers.sh
```

Das Script:
- Installiert Docker auf beiden Servern
- Richtet PostgreSQL, Redis, Meilisearch ein
- Erstellt alle Konfigurationsdateien
- Generiert sichere Passwörter
- Gibt die GitHub Secrets aus

### 2. GitHub Secrets einrichten

Gehe zu: `Repository Settings → Secrets and variables → Actions`

**Secrets hinzufügen:**

| Secret | Beschreibung |
|--------|--------------|
| `MASTER_IP` | IP des Master-Servers |
| `SLAVE_IP` | IP des Slave-Servers |
| `SSH_PRIVATE_KEY` | Inhalt von `~/.ssh/id_ed25519` |
| `POSTGRES_PASSWORD` | (vom Script generiert) |
| `REPLICATION_PASSWORD` | (vom Script generiert) |
| `SECRET_KEY` | (vom Script generiert) |
| `ENCRYPTION_MASTER_KEY` | (vom Script generiert) |
| `MEILISEARCH_KEY` | (vom Script generiert) |
| `SITE_URL` | `https://mandari.de` |

**Variable hinzufügen:**

| Variable | Wert |
|----------|------|
| `DEPLOYMENT_ENABLED` | `true` |

### 3. Deployment auslösen

```bash
git push origin main
```

Oder manuell: `Actions → Deploy Mandari → Run workflow`

## Server-Architektur

```
                    ┌─────────────────────┐
                    │   Load Balancer     │
                    │   (mandari.de)      │
                    └──────────┬──────────┘
                               │
              ┌────────────────┴────────────────┐
              │                                 │
              ▼                                 ▼
    ┌─────────────────┐              ┌─────────────────┐
    │     MASTER      │              │      SLAVE      │
    │                 │              │                 │
    │  • Caddy        │              │  • Caddy        │
    │  • Django API   │              │  • Django API   │
    │  • PostgreSQL   │◄────────────►│  • PostgreSQL   │
    │    (Primary)    │  Replication │    (Replica)    │
    │  • Redis        │              │  • Redis        │
    │  • Meilisearch  │              │  • Meilisearch  │
    │  • Ingestor     │              │  (kein Ingestor)│
    └─────────────────┘              └─────────────────┘
```

## Nützliche Befehle

### Status prüfen

```bash
# Auf Master
ssh root@MASTER_IP 'cd /opt/mandari && docker-compose ps'

# Logs anzeigen
ssh root@MASTER_IP 'cd /opt/mandari && docker-compose logs -f api'
```

### Manuelles Deployment

```bash
# Auf Server
cd /opt/mandari
docker-compose pull
docker-compose up -d
```

### Datenbank-Backup

```bash
ssh root@MASTER_IP 'docker exec mandari-postgres pg_dump -U mandari mandari > /opt/mandari/backup.sql'
```

### Django Shell

```bash
ssh root@MASTER_IP 'docker exec -it mandari-api python manage.py shell'
```

## Troubleshooting

### Container startet nicht

```bash
docker-compose logs <service-name>
```

### Datenbank-Verbindung fehlgeschlagen

```bash
# Prüfen ob PostgreSQL läuft
docker exec mandari-postgres pg_isready -U mandari
```

### Health Check fehlgeschlagen

```bash
curl -f http://localhost/health/
```

### SSL-Zertifikat Probleme

Caddy holt automatisch Let's Encrypt Zertifikate. Bei Problemen:

```bash
docker-compose logs caddy
```

## Passwörter zurücksetzen

Falls Passwörter verloren gegangen sind, können neue generiert werden:

```bash
# Neues Passwort generieren
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32

# In .env auf Server aktualisieren
nano /opt/mandari/.env

# Services neu starten
docker-compose down && docker-compose up -d
```

**Wichtig:** Auch die GitHub Secrets aktualisieren!
