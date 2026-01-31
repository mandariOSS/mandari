# Upgrading Guide

This guide covers updating Mandari to new versions.

## Before You Upgrade

1. **Read the changelog** - Check [CHANGELOG.md](../CHANGELOG.md) for breaking changes
2. **Create a backup** - Always backup before upgrading

```bash
./backup.sh
```

3. **Check system requirements** - New versions may have different requirements

## Quick Update

For minor updates (patches and features):

```bash
./update.sh
```

This script will:
1. Optionally create a backup
2. Pull latest Docker images
3. Restart services
4. Run database migrations

## Update to Specific Version

```bash
./update.sh v1.2.0
```

Or manually:

```bash
# Edit .env
sed -i 's/IMAGE_TAG=.*/IMAGE_TAG=v1.2.0/' .env

# Pull and restart
docker compose pull
docker compose up -d

# Run migrations
docker exec mandari-api python manage.py migrate
```

## Major Version Upgrades

Major versions (e.g., v1.x to v2.x) may require additional steps.

### General Process

1. **Backup everything**
   ```bash
   ./backup.sh
   cp -r . ../mandari-backup
   ```

2. **Read migration notes** in CHANGELOG.md

3. **Update configuration** if schema changed
   ```bash
   # Compare with new example
   diff .env .env.example
   ```

4. **Update docker-compose.yml** if structure changed
   ```bash
   git pull origin main
   ```

5. **Run upgrade**
   ```bash
   docker compose pull
   docker compose up -d
   docker exec mandari-api python manage.py migrate
   ```

6. **Verify**
   ```bash
   docker compose ps
   curl -sf http://localhost/health
   ```

## Rollback

If something goes wrong:

### Quick Rollback

```bash
# Stop services
docker compose down

# Restore previous version
./backup.sh --restore ./backups/mandari_backup_YYYYMMDD_HHMMSS.tar.gz

# Or just change version
sed -i 's/IMAGE_TAG=.*/IMAGE_TAG=v1.0.0/' .env
docker compose up -d
```

### Manual Rollback

```bash
# 1. Stop services
docker compose down

# 2. Restore .env
cp backup/.env .env

# 3. Set previous image version
echo "IMAGE_TAG=v1.0.0" >> .env

# 4. Start with old images
docker compose up -d

# 5. Restore database
docker exec -i mandari-postgres psql -U mandari mandari < backup/postgres.sql
```

## Git-Based Upgrades

If you cloned from Git:

```bash
# Save local changes
git stash

# Pull latest
git pull origin main

# Restore local changes
git stash pop

# Update containers
docker compose pull
docker compose up -d
docker exec mandari-api python manage.py migrate
```

## Upgrade Checklist

- [ ] Read CHANGELOG for breaking changes
- [ ] Create backup with `./backup.sh`
- [ ] Note current version: `docker compose exec api python manage.py version`
- [ ] Pull new images: `docker compose pull`
- [ ] Review any new environment variables
- [ ] Start new version: `docker compose up -d`
- [ ] Run migrations: `docker exec mandari-api python manage.py migrate`
- [ ] Verify health: `curl -sf https://your-domain.com/health`
- [ ] Check logs for errors: `docker compose logs -f`
- [ ] Test key functionality
- [ ] Clean up old images: `docker image prune -f`

## Automatic Updates (Optional)

### Using Watchtower

Watchtower can automatically update containers:

```yaml
# Add to docker-compose.override.yml
services:
  watchtower:
    image: containrrr/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 86400 --cleanup mandari-api mandari-ingestor
```

**Note**: This only updates Docker images, not configuration or migrations.

### Using Cron

Create `/etc/cron.daily/mandari-update`:

```bash
#!/bin/bash
cd /path/to/mandari
./update.sh --no-backup >> /var/log/mandari-update.log 2>&1
```

## Version Support

| Version | Status | Support Until |
|---------|--------|---------------|
| v2.x | Current | Active |
| v1.x | LTS | Dec 2025 |
| < v1.0 | EOL | None |

## Getting Help

If you encounter issues during upgrade:

1. Check logs: `docker compose logs`
2. Review [GitHub Issues](https://github.com/mandariOSS/mandari/issues)
3. Ask in [Discussions](https://github.com/mandariOSS/mandari/discussions)

When reporting issues, include:
- Previous version
- Target version
- Error messages from logs
- Steps to reproduce
