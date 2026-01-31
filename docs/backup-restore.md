# Backup & Restore Guide

This guide covers backing up and restoring Mandari data.

## What Gets Backed Up

| Component | Data | Importance |
|-----------|------|------------|
| PostgreSQL | All application data | **Critical** |
| .env file | Configuration & secrets | **Critical** |
| Redis | Session cache | Low (regenerated) |
| Meilisearch | Search index | Low (rebuild from DB) |
| Caddy | SSL certificates | Medium (auto-renewed) |

## Quick Backup

```bash
./backup.sh
```

Creates a backup in `./backups/` containing:
- PostgreSQL database dump
- Configuration file (.env)
- Metadata (timestamp, version)

## Backup Options

### Default (to ./backups/)

```bash
./backup.sh
```

### Custom Directory

```bash
./backup.sh /path/to/backup/directory
```

### Skip Backup (during update)

```bash
./update.sh --no-backup
```

## Restore

```bash
./backup.sh --restore ./backups/mandari_backup_20240115_120000.tar.gz
```

This will:
1. Stop all services
2. Restore .env configuration
3. Restore PostgreSQL database
4. Restart services
5. Optionally rebuild search index

## Manual Backup

### Database Only

```bash
docker exec mandari-postgres pg_dump -U mandari mandari > backup.sql
```

### With Compression

```bash
docker exec mandari-postgres pg_dump -U mandari mandari | gzip > backup.sql.gz
```

### Full Volume Backup

```bash
# Stop services first
docker compose down

# Backup PostgreSQL volume
docker run --rm -v mandari_postgres_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/postgres_volume.tar.gz -C /data .

# Restart
docker compose up -d
```

## Manual Restore

### Database Only

```bash
# Restore from SQL file
docker exec -i mandari-postgres psql -U mandari mandari < backup.sql

# From compressed
gunzip -c backup.sql.gz | docker exec -i mandari-postgres psql -U mandari mandari
```

### From Volume Backup

```bash
# Stop services
docker compose down

# Remove old data
docker volume rm mandari_postgres_data

# Create new volume and restore
docker volume create mandari_postgres_data
docker run --rm -v mandari_postgres_data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/postgres_volume.tar.gz -C /data

# Restart
docker compose up -d
```

## Scheduled Backups

### Using Cron

Create `/etc/cron.d/mandari-backup`:

```cron
# Daily backup at 2 AM
0 2 * * * root cd /opt/mandari && ./backup.sh /mnt/backups >> /var/log/mandari-backup.log 2>&1
```

### Using Systemd Timer

Create `/etc/systemd/system/mandari-backup.service`:

```ini
[Unit]
Description=Mandari Backup

[Service]
Type=oneshot
WorkingDirectory=/opt/mandari
ExecStart=/opt/mandari/backup.sh /mnt/backups
```

Create `/etc/systemd/system/mandari-backup.timer`:

```ini
[Unit]
Description=Daily Mandari Backup

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:
```bash
sudo systemctl enable mandari-backup.timer
sudo systemctl start mandari-backup.timer
```

## Off-site Backups

### To S3

```bash
# Install AWS CLI
apt install awscli

# Configure
aws configure

# Upload backup
aws s3 cp ./backups/mandari_backup_*.tar.gz s3://your-bucket/mandari/
```

### Automated S3 Sync

Add to backup script or create separate sync script:

```bash
#!/bin/bash
BACKUP_DIR="/opt/mandari/backups"
S3_BUCKET="s3://your-bucket/mandari-backups"

# Sync recent backups
aws s3 sync "$BACKUP_DIR" "$S3_BUCKET" \
  --exclude "*" \
  --include "*.tar.gz" \
  --storage-class STANDARD_IA
```

### To Remote Server (rsync)

```bash
rsync -avz ./backups/ user@backup-server:/backups/mandari/
```

## Backup Retention

The backup script automatically keeps only the last 7 backups. To change:

Edit `backup.sh` and modify:
```bash
# Keep only last 7 backups
ls -1t "$BACKUP_DIR"/*.tar.gz | tail -n +8 | xargs rm -f
```

Change `+8` to your preferred number + 1.

## Disaster Recovery

### Complete Server Loss

1. **Provision new server** with Docker installed

2. **Clone repository**
   ```bash
   git clone https://github.com/mandariOSS/mandari.git
   cd mandari
   ```

3. **Restore from backup**
   ```bash
   # Copy backup to new server
   scp backup-server:/backups/mandari/latest.tar.gz .

   # Restore
   ./backup.sh --restore latest.tar.gz
   ```

4. **Update DNS** to point to new server

5. **Verify** all services are working

### Recovery Time Objectives

| Scenario | RTO |
|----------|-----|
| Container restart | < 1 min |
| Image re-pull | < 5 min |
| Database restore (1GB) | < 10 min |
| Full server rebuild | < 30 min |

## Verification

### Test Backup Integrity

```bash
# Extract and check
tar -tzf ./backups/mandari_backup_*.tar.gz

# Test SQL file
zcat ./backups/mandari_backup_*/postgres.sql.gz | head -100
```

### Test Restore Process

**Recommended**: Periodically test restores on a separate server.

```bash
# On test server
git clone https://github.com/mandariOSS/mandari.git
cd mandari
./backup.sh --restore /path/to/backup.tar.gz
docker compose ps
curl http://localhost/health
```

## Troubleshooting

### "Database backup failed"

```bash
# Check if PostgreSQL is running
docker compose ps postgres

# Check PostgreSQL logs
docker compose logs postgres

# Try manual dump
docker exec mandari-postgres pg_dump -U mandari mandari
```

### "Restore failed"

```bash
# Check backup file
tar -tzf backup.tar.gz

# Extract manually
mkdir temp && tar -xzf backup.tar.gz -C temp
ls temp/
```

### Large Database Slow Restore

Use parallel restore:

```bash
# Dump with parallel
docker exec mandari-postgres pg_dump -U mandari -Fd -j 4 mandari -f /tmp/backup

# Restore with parallel
docker exec -i mandari-postgres pg_restore -U mandari -d mandari -j 4 /tmp/backup
```
