# Mandari Server Setup Guide

## Voraussetzungen

- **2x Hetzner VMs** (Ubuntu 24.04 empfohlen)
- **1x Hetzner Load Balancer** (zeigt auf beide VMs)
- **SSH-Zugang** zu beiden Servern (als root)
- **Domain** (z.B. mandari.de) zeigt auf Load Balancer IP

## Sicherheitsarchitektur

Mandari verwendet **End-to-End-Verschlüsselung** zwischen Browser und Server:

```
┌──────────┐     HTTPS      ┌──────────────────┐    TCP Passthrough    ┌────────────────┐
│  Browser │ ◄────────────► │  Hetzner Load    │ ◄──────────────────► │  Caddy + Let's │
│          │   TLS 1.3      │  Balancer        │    ProxyProtocol     │  Encrypt       │
└──────────┘                │  (TCP Mode)      │                      │  (Backend)     │
                            └──────────────────┘                      └────────────────┘
```

**Wichtig:** Der Load Balancer arbeitet im **TCP-Modus** (Passthrough), nicht HTTPS-Modus.
TLS wird am Backend (Caddy) terminiert, nicht am Load Balancer.

## Quick Start

### 1. Setup-Script ausführen

```bash
cd infrastructure/scripts

# Script ausführen (IPs sind bereits konfiguriert)
chmod +x setup-mandari.sh
./setup-mandari.sh
```

Das Script:
- Installiert Docker auf beiden Servern
- Richtet PostgreSQL, Redis, Meilisearch ein
- Erstellt alle Konfigurationsdateien
- Generiert sichere Passwörter
- Gibt die GitHub Secrets aus

### 2. Hetzner Load Balancer konfigurieren (TCP-Modus)

**WICHTIG:** Für End-to-End-Verschlüsselung muss der LB im TCP-Modus konfiguriert werden!

#### A. Services konfigurieren

Im Hetzner Cloud Console unter Load Balancer → Services:

| Service | Listen Port | Target Port | Protokoll | ProxyProtocol |
|---------|-------------|-------------|-----------|---------------|
| HTTPS   | 443         | 443         | **TCP**   | ✅ Aktiviert  |
| HTTP    | 80          | 80          | **TCP**   | ✅ Aktiviert  |

**Anleitung:**
1. Bestehende Services löschen (falls HTTPS-Modus)
2. Neuen Service erstellen:
   - Protokoll: `tcp` (NICHT https!)
   - Listen Port: `443`
   - Target Port: `443`
   - ProxyProtocol: `aktiviert`
3. Zweiten Service für HTTP erstellen:
   - Protokoll: `tcp`
   - Listen Port: `80`
   - Target Port: `80`
   - ProxyProtocol: `aktiviert`

#### B. Health Checks konfigurieren

Da TCP-Modus keine HTTP-Checks unterstützt, TCP-Health-Check verwenden:

| Setting | Wert |
|---------|------|
| Protokoll | TCP |
| Port | 443 |
| Interval | 10s |
| Timeout | 5s |
| Retries | 3 |

#### C. Targets hinzufügen

Beide VMs als Targets mit **Private IPs** hinzufügen:
- Master: `10.0.0.3`
- Slave: `10.0.0.4`

**Hinweis:** Das Managed Certificate am Load Balancer wird im TCP-Modus NICHT verwendet.
Caddy holt automatisch Let's Encrypt Zertifikate für die Domain.

### 3. GitHub Secrets einrichten

Gehe zu: `Repository Settings → Secrets and variables → Actions`

**Secrets hinzufügen:**

| Secret | Beschreibung |
|--------|--------------|
| `MASTER_IP` | IP des Master-Servers (46.225.61.128) |
| `SLAVE_IP` | IP des Slave-Servers (46.225.58.145) |
| `SSH_PRIVATE_KEY` | Inhalt von `~/.ssh/id_hetzner` |
| `GHCR_TOKEN` | GitHub Personal Access Token (siehe unten) |
| `POSTGRES_PASSWORD` | (vom Script generiert) |
| `SECRET_KEY` | (vom Script generiert) |
| `ENCRYPTION_MASTER_KEY` | (vom Script generiert) |
| `MEILISEARCH_KEY` | (vom Script generiert) |
| `SITE_URL` | `https://mandari.de` |

**GHCR_TOKEN erstellen:**

Der GHCR_TOKEN ermöglicht den Servern, Docker-Images aus dem GitHub Container Registry zu laden.

1. Gehe zu: https://github.com/settings/tokens/new
2. Wähle: **"Generate new token (classic)"**
3. Setze:
   - Note: `mandari-deployment`
   - Expiration: `No expiration` (oder nach Bedarf)
   - Scopes: Nur `read:packages` ✅
4. Klicke "Generate token"
5. Kopiere den Token und füge ihn als `GHCR_TOKEN` Secret hinzu

**Alternativ:** Wenn die Packages öffentlich sind, ist kein Token erforderlich.

**Variable hinzufügen:**

| Variable | Wert |
|----------|------|
| `DEPLOYMENT_ENABLED` | `true` |

### 4. Deployment auslösen

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
ssh root@MASTER_IP 'cd /opt/mandari && docker compose ps'

# Logs anzeigen
ssh root@MASTER_IP 'cd /opt/mandari && docker compose logs -f api'
```

### Manuelles Deployment

```bash
# Auf Server
cd /opt/mandari
docker compose pull
docker compose up -d
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
docker compose logs <service-name>
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
docker compose logs caddy
```

**Häufige Ursachen:**
- Domain DNS zeigt nicht auf Load Balancer IP
- Load Balancer blockiert Port 443 TCP
- Let's Encrypt Rate Limit erreicht (max 5 Zertifikate pro Domain/Woche)

### Load Balancer zeigt "Unhealthy"

1. Prüfen ob TCP-Modus konfiguriert ist (nicht HTTPS)
2. Prüfen ob ProxyProtocol aktiviert ist
3. Caddy Logs prüfen:
```bash
docker compose logs caddy
```

4. Manueller Test vom LB-Netzwerk:
```bash
# Auf dem Server
curl -I http://localhost/health
```

### ProxyProtocol Fehler

Falls Caddy mit "proxy protocol error" abstürzt:
- ProxyProtocol im Load Balancer aktiviert?
- Wenn LB kein ProxyProtocol sendet, Caddy-Config anpassen

## Passwörter zurücksetzen

Falls Passwörter verloren gegangen sind, können neue generiert werden:

```bash
# Neues Passwort generieren
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32

# In .env auf Server aktualisieren
nano /opt/mandari/.env

# Services neu starten
docker compose down && docker compose up -d
```

**Wichtig:** Auch die GitHub Secrets aktualisieren!
