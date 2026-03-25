# Mandari 2.0 - Deployment Guide

## Übersicht

Es gibt **3 Wege** zum Deployment:

| Methode | Wann nutzen | Automatisierung |
|---------|-------------|-----------------|
| **GitHub Actions** | Empfohlen für Produktion | Vollautomatisch |
| **Make Commands** | Lokales Deployment / Debugging | Semi-automatisch |
| **Shell Scripts** | Server-Zugriff / Notfälle | Manuell |

---

## 🚀 Option 1: GitHub Actions (Empfohlen)

### Automatisches Deployment bei Push

Jeder Push auf `main` oder `production` löst automatisch aus:
1. Tests laufen
2. Docker Images werden gebaut
3. Images werden zu GitHub Container Registry gepusht
4. Ansible deployed auf die Server

```
git add .
git commit -m "Feature: Neue Funktion"
git push origin main
# → Deployment startet automatisch!
```

### Manuelles Deployment

1. Gehe zu **Actions** → **Deploy Mandari**
2. Klicke **Run workflow**
3. Wähle Environment (`staging` oder `production`)
4. Klicke **Run workflow**

### Erforderliche GitHub Secrets

Gehe zu **Settings** → **Secrets and variables** → **Actions** und füge hinzu:

| Secret | Beschreibung | Beispiel |
|--------|--------------|----------|
| `SSH_PRIVATE_KEY` | SSH Key für Server-Zugriff | `-----BEGIN OPENSSH...` |
| `MASTER_IP` | IP des Master-Servers | `168.119.xxx.xxx` |
| `SLAVE_IP` | IP des Slave-Servers | `168.119.xxx.xxx` |
| `SITE_URL` | Produktions-URL | `https://mandari.de` |
| `SECRET_KEY` | Django Secret Key | (generiert) |
| `ENCRYPTION_MASTER_KEY` | Verschlüsselungs-Key | (generiert) |
| `POSTGRES_USER` | DB Benutzer | `mandari` |
| `POSTGRES_PASSWORD` | DB Passwort | (generiert) |
| `POSTGRES_DB` | DB Name | `mandari` |
| `REPLICATION_PASSWORD` | Replikations-Passwort | (generiert) |
| `ELASTICSEARCH_URL` | Elasticsearch URL | `http://elasticsearch:9200` |

**Secrets generieren:**
```bash
make secrets-generate
```

---

## 🛠️ Option 2: Make Commands (Lokal)

### Voraussetzungen

```bash
# macOS
brew install terraform ansible

# Oder mit pip
pip install ansible ansible-lint

# Ansible Dependencies
make ansible-deps
```

### Erstes Deployment

```bash
# 1. Terraform konfigurieren
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
# → Hetzner API Token eintragen

# 2. Environment konfigurieren
cd ../docker
cp .env.example .env
# → Alle Secrets eintragen (make secrets-generate hilft)

# 3. Vollständiges Deployment
make deploy-full
```

### Alltägliches Deployment

```bash
# Nur App deployen (Infrastruktur existiert schon)
make deploy

# Status prüfen
make status

# Logs anschauen
make logs
make logs-api
make logs-ingestor
```

### Alle verfügbaren Commands

```bash
make help
```

Wichtige Commands:
| Command | Beschreibung |
|---------|--------------|
| `make deploy` | App deployen |
| `make deploy-full` | Infra + Setup + App |
| `make status` | Deployment-Status |
| `make logs` | Live-Logs |
| `make ssh-master` | SSH zum Master |
| `make backup` | Backup erstellen |
| `make db-replication` | Replikations-Status |

---

## 📜 Option 3: Shell Scripts (Direkt)

### Auf dem Server

```bash
# SSH zum Master
ssh root@<MASTER_IP>

# Deployment
cd /opt/mandari
docker compose pull
docker compose up -d

# Logs
docker compose logs -f

# Status
docker ps
```

### Mit Scripts

```bash
cd infrastructure/scripts

# Deployment
./deploy.sh app

# Backup
./backup.sh full

# Failover prüfen
./failover.sh check
```

---

## 📋 Deployment Checkliste

### Vor dem ersten Deployment

- [ ] Hetzner Cloud Account mit API Token
- [ ] Domain (mandari.de) mit DNS-Zugriff
- [ ] SSH Key generiert (`ssh-keygen -t ed25519`)
- [ ] GitHub Secrets konfiguriert
- [ ] `terraform.tfvars` ausgefüllt
- [ ] `.env` mit allen Secrets

### Nach dem Deployment

- [ ] `make status` zeigt alle Services als "healthy"
- [ ] https://mandari.de/health erreichbar
- [ ] `make db-replication` zeigt aktive Replikation
- [ ] Backup funktioniert (`make backup`)

---

## 🔄 Typische Workflows

### Feature deployen

```bash
# 1. Lokal entwickeln
cd mandari
python manage.py runserver

# 2. Tests
make test

# 3. Commit & Push
git add .
git commit -m "Feature: XYZ"
git push origin main

# 4. GitHub Action läuft automatisch
# → Warte auf grünes Häkchen
```

### Hotfix deployen

```bash
# Schnelles Deployment ohne Tests
# GitHub Actions → Run workflow → skip_tests: true

# Oder manuell:
make deploy
```

### Rollback

```bash
# Backups auflisten
make backup-list

# Rollback zu bestimmtem Backup
make rollback BACKUP=deploy-1234567890.tar.gz
```

### Datenbank-Migration

```bash
# Migrationen werden automatisch bei Deploy ausgeführt

# Manuell:
ssh root@<MASTER_IP>
docker exec mandari-api python manage.py migrate
```

---

## 🚨 Troubleshooting

### Deployment schlägt fehl

```bash
# 1. Logs prüfen
make logs-api

# 2. Container-Status
ssh root@<MASTER_IP>
docker ps -a
docker logs mandari-api

# 3. Health-Check manuell
curl http://<MASTER_IP>/health
```

### PostgreSQL Replikation kaputt

```bash
# Status prüfen
make db-replication

# Replikation neu initialisieren
ssh root@<SLAVE_IP>
/opt/mandari/scripts/init-replica.sh
```

### Container startet nicht

```bash
ssh root@<MASTER_IP>

# Logs anschauen
docker logs mandari-api

# Container neu starten
docker compose restart api

# Alles neu starten
docker compose down && docker compose up -d
```

---

## 📊 Monitoring

### Basis-Monitoring

```bash
# Server-Status
make status

# Live-Logs
make logs

# Replikation
make db-replication
```

### Health-Endpoints

| Endpoint | Beschreibung |
|----------|--------------|
| `/health` | Allgemeiner Health-Check |
| `/api/health` | API Health |

### Metriken (optional)

Für erweiteres Monitoring empfohlen:
- **Hetzner Cloud Console** - CPU, RAM, Netzwerk
- **Sentry** - Error Tracking
- **Prometheus + Grafana** - Metriken

---

## 💰 Kosten

| Ressource | Typ | Monatlich |
|-----------|-----|-----------|
| 2× VM | cx31 | €31.18 |
| 1× Load Balancer | lb11 | €5.39 |
| 2× Volume | 50GB | €4.80 |
| **Gesamt** | | **~€42** |

---

## 🔒 Sicherheit

- SSH nur mit Key-Auth (kein Passwort)
- Firewall (UFW) auf allen Servern
- fail2ban gegen Brute-Force
- TLS-Terminierung am Load Balancer
- Alle Secrets in GitHub Secrets / .env (nie im Code!)
- Daten verschlüsselt (AES-256-GCM)
