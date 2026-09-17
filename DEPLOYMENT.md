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

### Mit Skript: Gesundheitsprüfung und automatischer Rückfall

`deploy/scripts/deploy.sh` ist nicht interaktiv (für Betrieb, Cron, CI) und macht aus
jedem Deploy einen geprüften Vorgang: Sicherung, `safemigrate`, Umschalten mit `--wait`,
dann **Anwendungsprüfung im Container** (`deploy/scripts/verify_deploy.py`: Readiness,
Anmeldeseite, Bürgerportal, OParl-System, optional angemeldete Demo-Seiten, jeweils mit
Inhaltsprüfung). Scheitert sie, schaltet das Skript **selbsttätig auf das vorherige Image
zurück** und meldet das per Mail. Jeder Lauf schreibt eine Zeile in `deploy-log.tsv`
(alt, neu, Ergebnis, Unterbrechung in Sekunden, Dauer), die Grundlage für die Kennzahl
„Ausfallzeit je Deploy“ aus dem Verfügbarkeitskonzept.

```bash
# Umgebung einmalig in einer Datei ablegen (Dienstnamen, Compose-Dateien, Empfänger)
set -a; . /opt/mandari/deploy.env; set +a
sh deploy/scripts/deploy.sh plan   v0.11.0   # Images ziehen, migrate --plan, check
sh deploy/scripts/deploy.sh apply  v0.11.0   # Sicherung, Migration, Umschalten, Prüfung, ggf. Rückfall
sh deploy/scripts/deploy.sh verify           # nur die Prüfung gegen den laufenden Stand
sh deploy/scripts/deploy.sh rollback v0.10.0 # von Hand zurück
```

Alle Parameter (`MANDARI_DIR`, `COMPOSE_FILES`, `APP_SERVICE`, `WORKER_SERVICES`,
`DB_SERVICE`, `BACKUP_DIR`, `NOTIFY_EMAIL`, `VERIFY_*`) stehen im Kopf des Skripts.
Migrationen müssen abwärtskompatibel sein: Der Rückfall rollt Code zurück, keine
Migrationen (`django-safemigrate` spielt nur verträgliche Migrationen vor dem Umschalten ein).
Das interaktive `update.sh` für Selbst-Hoster nutzt dieselbe Anwendungsprüfung und rollt
bei Fehlschlag ebenfalls zurück.

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

## 🐘 PostgreSQL: Grundeinstellungen

PostgreSQL liefert Werte aus, die zu einem kleinen Rechner mit drehender
Festplatte passen. Für mandari trifft beides nicht zu: `shared_buffers=128MB`
gegen einen Datenbestand von mehreren Gigabyte bedeutet, dass eine einzige
größere Abfrage den gesamten Cache verdrängt. `random_page_cost=4` beschreibt
eine Festplatte und führt dazu, dass der Planer Indizes meidet und stattdessen
ganze Tabellen liest.

Die mitgelieferte `docker-compose.yml` setzt deshalb eigene Werte. **Jeder ist
per Umgebungsvariable übersteuerbar**, und eine Änderung wirkt erst nach einem
Neustart des Datenbankdienstes.

### Woher die Werte kommen

Alles leitet sich vom Arbeitsspeicher des **Datenbankdienstes** ab
(`POSTGRES_MEM_LIMIT`, Vorgabe `1g`) — nicht vom Arbeitsspeicher des Servers.

| Einstellung | Faustregel | Vorgabe bei 1 GB |
|---|---|---|
| `POSTGRES_SHARED_BUFFERS` | **ein Viertel** des Dienst-Speichers | `256MB` |
| `POSTGRES_EFFECTIVE_CACHE_SIZE` | **drei Viertel** — eine Schätzung, keine Belegung: was der Planer an Cache erwarten darf | `768MB` |
| `POSTGRES_WORK_MEM` | je Sortier- oder Gruppiervorgang, **nicht je Verbindung** | `8MB` |
| `POSTGRES_MAINTENANCE_WORK_MEM` | für `VACUUM` und Indexaufbau, etwa ein Achtel | `128MB` |
| `POSTGRES_RANDOM_PAGE_COST` | `1.1` für SSD/NVMe, `4` nur für drehende Platten | `1.1` |
| `POSTGRES_EFFECTIVE_IO_CONCURRENCY` | `200` für SSD/NVMe, `2` für drehende Platten | `200` |
| `POSTGRES_MAX_CONNECTIONS` | muss zur Summe aller Dienste passen | `100` |
| `POSTGRES_MAX_WAL_SIZE` | mehr bedeutet seltenere Checkpoints | `2GB` |

**Die Falle bei `work_mem`:** Der Wert gilt je Sortiervorgang, und eine einzelne
Abfrage kann mehrere davon haben. Im Extremfall belegt die Datenbank
`max_connections × work_mem` zusätzlich zu `shared_buffers`. Bei 100 Verbindungen
und 8 MB sind das rechnerisch 800 MB — deshalb sind `mem_limit`,
`max_connections` und `work_mem` nur gemeinsam zu ändern.

Größenempfehlungen je Größenklasse (klein, mittel, groß) mit Mengengerüst,
Verbindungsbudget und den Ergebnissen der Lasttests: [docs/LASTTESTS.md](docs/LASTTESTS.md).

### Für größere Installationen

Beispiel aus dem eigenen Betrieb (Datenbankdienst mit 4 GB, gemeinsam genutzt von
mandari, Website und Kundenportal):

```env
POSTGRES_MEM_LIMIT=4g
POSTGRES_SHARED_BUFFERS=1GB
POSTGRES_EFFECTIVE_CACHE_SIZE=3GB
POSTGRES_WORK_MEM=8MB
POSTGRES_MAINTENANCE_WORK_MEM=256MB
POSTGRES_MAX_CONNECTIONS=200
```

### Wirkung

Gemessen am eigenen Bestand (Köln, 42.247 Vorgänge), zusammen mit den Indizes
aus #256:

| Seite | vorher | nachher |
|---|---|---|
| Insight-Portal, extern gemessen | 204 ms | **46 ms** |
| Auswahlseite, serverseitig | 193 ms | **12 ms** |
| Kommunenseite Köln | 215 ms | **20 ms** |

Listen- und Detailseiten lagen vorher wie nachher bei 10–50 ms; sie sind durch
die Umstellung nicht langsamer geworden.

## ⏰ Geplante Aufgaben (Cron)

Die Anwendung bringt keinen eigenen Scheduler mit. Wiederkehrende Management-Commands
laufen auf dem Host per Cron gegen den laufenden Container, jeweils mit eigener Logdatei:

```cron
# Erinnerungen und Pflege (Beispiel; Containername anpassen)
0 7 * * *   docker exec mandari-app python manage.py send_session_reminders   >> /var/log/mandari-reminders.log 2>&1
30 7 * * *  docker exec mandari-app python manage.py send_question_reminders  >> /var/log/mandari-question-reminders.log 2>&1
15 7 * * *  docker exec mandari-app python manage.py send_task_due_reminders  >> /var/log/mandari-task-reminders.log 2>&1
0 3 * * 1   docker exec mandari-app python manage.py fetch_person_photos      >> /var/log/mandari-person-photos.log 2>&1
# Verwaiste Konten (unbestätigt, abgelehnt, ohne Zuordnung) nach Frist löschen, Issue #238
45 3 * * *  docker exec mandari-app python manage.py cleanup_orphaned_accounts >> /var/log/mandari-orphaned-accounts.log 2>&1
```

Vor dem ersten Scharfschalten von `cleanup_orphaned_accounts` lohnt ein Probelauf mit
`--dry-run`; die Kriterien stehen in `docs/DSGVO_LOESCHKONZEPT.md`. Die Ausgaben aller
Läufe enthalten nur Zahlen, keine personenbezogenen Daten.

## 🔌 Datenbankverbindungen: Budget

Alle Dienste teilen sich **eine** PostgreSQL-Instanz. Ist deren `max_connections`
erschöpft, bekommt *jeder* Dienst `FATAL: sorry, too many clients already` — auch
einer ganz ohne eigene Last. Genau das ist am 15.09.2026 passiert, als ein
Schwachstellen-Scanner mit 304 Anfragen pro Minute (normal: 8) die Verbindungszahl
über die damalige Obergrenze trieb.

Deshalb hat jeder Dienst eine feste Obergrenze, und die Summe bleibt unter der
Obergrenze der Datenbank.

| Dienst | Obergrenze | Woher |
|---|---|---|
| mandari (Daphne, 1 Prozess) | **10** | `DB_POOL_MAX`, passt zum ASGI-Thread-Pool von Django |
| Ingestor | 30 | SQLAlchemy `pool_size=10` + `max_overflow=20` |
| OCR-Worker | 30 | gleiches Image wie der Ingestor |
| Website (Wagtail) | 10 | eigener Container, eigene Datenbank |
| Kundenportal | 10 | eigener Container, eigene Datenbank |
| Sicherung (`pg_dump`) | 2 | nur während des Laufs |
| Reserve für Superuser | 3 | `superuser_reserved_connections`, Postgres-Vorgabe |
| **Summe** | **95** | |
| **`max_connections`** | **200** | in `docker-compose.web01.yml` |

Reserve: rund 100 Verbindungen. Wer einen Dienst hinzufügt, trägt ihn hier ein
**und** prüft die Summe.

### Einstellungen der Anwendung

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `DB_POOL` | `true` | Pool an- oder abschalten. Bei SQLite ohne Wirkung |
| `DB_POOL_MIN` | `2` | Vorgehaltene Verbindungen, damit die erste Anfrage nicht wartet |
| `DB_POOL_MAX` | `10` | Obergrenze je Prozess |
| `DB_POOL_TIMEOUT` | `10` | Sekunden warten bei erschöpftem Pool, statt sofort zu scheitern |
| `DB_POOL_MAX_LIFETIME` | `1800` | Verbindungen nach dieser Zeit erneuern |

Bei eingeschaltetem Pool setzt die Anwendung `CONN_MAX_AGE` selbsttätig auf `0` —
Django verlangt das, weil sonst zwei Mechanismen dieselbe Verbindung verwalten
würden.

### Prüfen, was tatsächlich offen ist

```bash
docker exec staging-postgres psql -U mandari -d postgres -c "
select datname, count(*) as verbindungen, count(*) filter (where state='idle') as idle
from pg_stat_activity where backend_type='client backend' group by 1 order by 2 desc;"
```

Gemessener Normalbetrieb (16.09.2026): mandari 13, Portal 3, Website 2.

### Wenn der Ingestor der Engpass wird

Der Ingestor darf mit 30 Verbindungen mehr als die Anwendung. Das ist historisch
und nicht gemessen — wer hier Luft braucht, kürzt zuerst dort
(`ingestor/src/storage/database.py`, `pool_size` und `max_overflow`).

## 🧾 Protokolle

Container-Logs laufen auf Produktionshosts über `journald` (90 Tage, höchstens 2 GB,
überstehen die Neuerstellung von Containern), Zugriffslogs von Caddy 14 Tage, das
fachliche Audit-Log je Mandant in der Datenbank. Einrichtung in drei Schritten:

```bash
sudo sh deploy/logging/install.sh mandari admin@example.org      # journald-Drop-in, logrotate, Überlaufwarnung
docker compose -f docker-compose.yml -f deploy/logging/docker-compose.journald.yml up -d
# Caddy: roll_keep_for 336h in den Zugriffslog-Blöcken, dann caddy reload
```

Fristen, Begründungen, Sicherung und Zugriffsschutz: [docs/PROTOKOLLE.md](docs/PROTOKOLLE.md).

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
