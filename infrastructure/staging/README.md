# Mandari Staging Environment

Docker-basierte Staging-Umgebung mit **Let's Encrypt SSL** für `mandari.dev`.

## Voraussetzungen

1. **Server mit öffentlicher IP** - Let's Encrypt muss die Domain validieren können
2. **Domain-Konfiguration** - `mandari.dev` muss auf die Server-IP zeigen
3. **Offene Ports** - Port 80 und 443 müssen erreichbar sein
4. **Docker & Docker Compose** installiert

## Schnellstart

### 1. DNS-Eintrag einrichten

Stelle sicher, dass `mandari.dev` und `www.mandari.dev` auf deine Server-IP zeigen:

```
mandari.dev.     A    <DEINE-SERVER-IP>
www.mandari.dev. A    <DEINE-SERVER-IP>
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.staging .env
nano .env  # Passe SECRET_KEY, POSTGRES_PASSWORD etc. an
```

### 3. SSL-Zertifikat initialisieren (ERSTER START)

**Beim ersten Start** muss das Let's Encrypt Zertifikat eingerichtet werden:

```bash
# Linux/macOS
chmod +x init-letsencrypt.sh
./init-letsencrypt.sh

# Windows (Git Bash oder WSL)
bash init-letsencrypt.sh
```

Das Script:
- Erstellt ein temporäres Zertifikat für nginx
- Startet nginx
- Fordert ein echtes Let's Encrypt Zertifikat an
- Lädt nginx mit dem neuen Zertifikat neu

### 4. Alle Services starten

```bash
docker-compose -f docker-compose.staging.yml up -d
```

### 5. Datenbank migrieren

```bash
docker exec mandari-staging-django python manage.py migrate
```

## Zugriff

| Service | URL |
|---------|-----|
| Web Application | https://mandari.dev |
| Django Admin | https://mandari.dev/admin/ |
| Health Check | https://mandari.dev/health/ |
| pgAdmin (optional) | http://localhost:5050 |

## Services

| Service | Port | Beschreibung |
|---------|------|--------------|
| nginx | 80, 443 | Reverse Proxy mit SSL |
| django | 8000 (intern) | Django Anwendung |
| postgres | 5432 (intern) | PostgreSQL Datenbank |
| redis | 6379 (intern) | Cache & Sessions |
| meilisearch | 7700 (intern) | Volltextsuche |
| certbot | - | SSL-Zertifikat-Erneuerung |

## Befehle

```bash
# Starten
docker-compose -f docker-compose.staging.yml up -d

# Stoppen
docker-compose -f docker-compose.staging.yml down

# Logs anzeigen
docker-compose -f docker-compose.staging.yml logs -f

# Einzelne Service-Logs
docker-compose -f docker-compose.staging.yml logs -f django
docker-compose -f docker-compose.staging.yml logs -f nginx

# Neu bauen
docker-compose -f docker-compose.staging.yml build

# Mit pgAdmin starten
docker-compose -f docker-compose.staging.yml --profile tools up -d

# Django Shell
docker exec -it mandari-staging-django python manage.py shell

# Migrationen ausführen
docker exec mandari-staging-django python manage.py migrate

# Superuser erstellen
docker exec -it mandari-staging-django python manage.py createsuperuser
```

## SSL-Zertifikat

### Automatische Erneuerung

Certbot läuft als Container und erneuert Zertifikate automatisch alle 12 Stunden (wenn nötig).

### Manuelle Erneuerung

```bash
docker-compose -f docker-compose.staging.yml run --rm certbot renew
docker-compose -f docker-compose.staging.yml exec nginx nginx -s reload
```

### E-Mail-Konfiguration

Bearbeite `init-letsencrypt.sh` und setze deine E-Mail-Adresse:

```bash
email="deine-email@example.com"
```

Let's Encrypt sendet Warnungen vor Zertifikatsablauf an diese Adresse.

## Daten-Backup

```bash
# Datenbank exportieren
docker exec mandari-staging-postgres pg_dump -U mandari mandari > backup.sql

# Datenbank importieren
cat backup.sql | docker exec -i mandari-staging-postgres psql -U mandari mandari

# Django-Daten exportieren
docker exec mandari-staging-django python manage.py dumpdata \
    --natural-foreign --natural-primary \
    -e contenttypes -e auth.Permission \
    --indent 2 > data.json

# Django-Daten importieren
docker exec -i mandari-staging-django python manage.py loaddata /dev/stdin < data.json
```

## Architektur

```
┌─────────────────────────────────────────────────────────────┐
│              https://mandari.dev (Port 443)                  │
│                  (Let's Encrypt SSL)                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                         NGINX                                │
│              (Reverse Proxy + SSL Termination)               │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
    /static/             /media/                  /
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   Static    │      │   Media     │      │   Django    │
│   Files     │      │   Files     │      │  (Gunicorn) │
└─────────────┘      └─────────────┘      └─────────────┘
                                                 │
                     ┌───────────────────────────┼───────────────────────────┐
                     │                           │                           │
                     ▼                           ▼                           ▼
              ┌─────────────┐            ┌─────────────┐            ┌─────────────┐
              │ PostgreSQL  │            │    Redis    │            │ Meilisearch │
              │  (Port 5432)│            │ (Port 6379) │            │ (Port 7700) │
              └─────────────┘            └─────────────┘            └─────────────┘
```

## Troubleshooting

### Zertifikat-Fehler

Falls das Zertifikat nicht erstellt werden kann:

1. **DNS prüfen:** `nslookup mandari.dev` - muss deine Server-IP zurückgeben
2. **Firewall prüfen:** Ports 80 und 443 müssen von außen erreichbar sein
3. **Certbot-Logs:** `docker-compose -f docker-compose.staging.yml logs certbot`

### Nginx startet nicht

```bash
# Konfiguration testen
docker exec mandari-staging-nginx nginx -t

# Logs prüfen
docker-compose -f docker-compose.staging.yml logs nginx
```

### Django-Fehler

```bash
docker-compose -f docker-compose.staging.yml logs django
```

### Port bereits belegt

```bash
# Linux/macOS
sudo lsof -i :80
sudo lsof -i :443

# Windows
netstat -ano | findstr :80
netstat -ano | findstr :443
```

### Container-Status

```bash
docker-compose -f docker-compose.staging.yml ps
```

### Datenbank-Verbindung

```bash
docker exec mandari-staging-postgres pg_isready -U mandari
docker exec -it mandari-staging-postgres psql -U mandari mandari
```
