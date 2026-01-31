# Configuration Guide

This guide covers all configuration options for Mandari Community Edition.

## Environment Variables

All configuration is done through the `.env` file. The installer creates this automatically, but you can modify it at any time.

After changing `.env`, restart services:
```bash
docker compose down
docker compose up -d
```

## Required Settings

### Domain & SSL

```bash
# Your domain (e.g., mandari.example.com)
DOMAIN=mandari.example.com

# Email for Let's Encrypt notifications
ACME_EMAIL=admin@example.com
```

For local development without SSL:
```bash
DOMAIN=localhost
```

### Database

```bash
POSTGRES_USER=mandari
POSTGRES_PASSWORD=your-secure-password
POSTGRES_DB=mandari
```

### Security Keys

```bash
# Django secret key (min. 50 chars)
SECRET_KEY=your-secret-key

# Encryption key for sensitive data
ENCRYPTION_MASTER_KEY=your-base64-encoded-32-byte-key

# Meilisearch API key
MEILISEARCH_KEY=your-meilisearch-key
```

**Important**: Never change `ENCRYPTION_MASTER_KEY` after installation - encrypted data will become unreadable.

## Optional Settings

### Timezone

```bash
TZ=Europe/Berlin
```

Common values:
- `Europe/Berlin` (Germany)
- `Europe/Vienna` (Austria)
- `Europe/Zurich` (Switzerland)
- `UTC` (Coordinated Universal Time)

### Resource Limits

```bash
# Redis memory limit
REDIS_MAXMEMORY=256mb
```

Recommended values:
- Small (< 10 bodies): `256mb`
- Medium (10-50 bodies): `512mb`
- Large (50+ bodies): `1gb`

### OParl Ingestor

```bash
# Sync interval in minutes
INGESTOR_INTERVAL=15

# Hour for daily full sync (0-23)
INGESTOR_FULL_SYNC_HOUR=3

# Concurrent sync workers
INGESTOR_CONCURRENT=10
```

Tuning tips:
- Increase `INGESTOR_INTERVAL` to reduce load
- Run full sync during off-peak hours
- Reduce `INGESTOR_CONCURRENT` if experiencing rate limits

### Docker Images

```bash
# Version tag to deploy
IMAGE_TAG=latest
```

Options:
- `latest` - Most recent stable release
- `v1.2.3` - Specific version
- `main` - Development branch (unstable)

## Email Configuration

Email can be configured via environment variables or Django Admin.

### Via Environment

```bash
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-username
EMAIL_HOST_PASSWORD=your-password
EMAIL_USE_TLS=true
DEFAULT_FROM_EMAIL=noreply@example.com
```

### Via Django Admin

1. Go to Admin > Site Settings
2. Configure SMTP settings
3. Send a test email

### Common Providers

**Gmail (not recommended for production)**:
```bash
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
```

**Mailgun**:
```bash
EMAIL_HOST=smtp.mailgun.org
EMAIL_PORT=587
EMAIL_USE_TLS=true
```

**AWS SES**:
```bash
EMAIL_HOST=email-smtp.eu-central-1.amazonaws.com
EMAIL_PORT=587
EMAIL_USE_TLS=true
```

## AI Services (Optional)

Enable AI-powered features like summaries and chat:

```bash
# Groq (fast, cost-effective)
GROQ_API_KEY=your-groq-api-key

# OpenAI (alternative)
OPENAI_API_KEY=your-openai-api-key
```

## Monitoring (Optional)

### Sentry (Error Tracking)

```bash
SENTRY_DSN=https://xxx@sentry.io/xxx
```

## Caddyfile Customization

The `Caddyfile` can be modified for advanced routing needs.

### Add Custom Headers

```caddyfile
{$DOMAIN} {
    header {
        # Your custom headers
        X-Custom-Header "value"
    }
    # ... rest of config
}
```

### Enable Basic Auth for Admin

```caddyfile
handle /admin/* {
    basicauth {
        admin $2a$14$hash...
    }
    reverse_proxy api:8000
}
```

Generate password hash:
```bash
docker exec mandari-caddy caddy hash-password
```

### Rate Limiting

```caddyfile
handle /api/* {
    rate_limit {
        zone api_limit {
            key {remote_host}
            events 100
            window 1m
        }
    }
    reverse_proxy api:8000
}
```

## Docker Compose Overrides

Create `docker-compose.override.yml` for local customizations:

```yaml
services:
  api:
    environment:
      DEBUG: "true"
    volumes:
      - ./mandari:/app  # Mount local code
```

This file is automatically loaded and won't be overwritten by updates.

## Performance Tuning

### For Large Installations

```bash
# Increase Redis memory
REDIS_MAXMEMORY=1gb

# More concurrent syncs
INGESTOR_CONCURRENT=20
```

### PostgreSQL Tuning

Create `docker-compose.override.yml`:

```yaml
services:
  postgres:
    command:
      - "postgres"
      - "-c"
      - "shared_buffers=512MB"
      - "-c"
      - "effective_cache_size=2GB"
      - "-c"
      - "work_mem=32MB"
```

## Security Hardening

### Restrict Admin Access

In `Caddyfile`, limit admin access by IP:

```caddyfile
handle /admin/* {
    @allowed remote_ip 10.0.0.0/8 192.168.0.0/16
    handle @allowed {
        reverse_proxy api:8000
    }
    respond "Forbidden" 403
}
```

### Disable Debug Mode

Ensure in `.env`:
```bash
DEBUG=false
```

### Regular Updates

```bash
./update.sh
```

## Troubleshooting

### Check Configuration

```bash
# Validate docker-compose.yml
docker compose config

# Check environment
docker compose run --rm api env | grep -E "SECRET|DATABASE|REDIS"
```

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f api
```

### Reset to Defaults

```bash
# Keep data, reset config
cp .env.example .env
# Edit with your values
docker compose up -d
```
