#!/bin/bash
set -e

# =============================================================================
# Mandari - Docker Entrypoint
# =============================================================================
# Optimiert für schnellen Startup (Zero-Downtime Updates):
#   - collectstatic nur wenn nötig
#   - Meilisearch-Setup im Hintergrund
#   - Gunicorn mit --preload für schnelleren Worker-Start
# =============================================================================

# Function to wait for database
wait_for_db() {
    echo "Waiting for database to be ready..."
    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if python -c "
import os
import sys
try:
    import psycopg
    # Parse DATABASE_URL
    url = os.environ.get('DATABASE_URL', '')
    # Convert asyncpg URL to psycopg format
    url = url.replace('postgresql+asyncpg://', 'postgresql://')
    conn = psycopg.connect(url, connect_timeout=5)
    conn.close()
    sys.exit(0)
except Exception as e:
    print(f'Database not ready: {e}')
    sys.exit(1)
" 2>/dev/null; then
            echo "Database is ready!"
            return 0
        fi

        echo "Attempt $attempt/$max_attempts: Database not ready, waiting 2 seconds..."
        sleep 2
        attempt=$((attempt + 1))
    done

    echo "ERROR: Database not ready after $max_attempts attempts"
    return 1
}

# Static Files: nur wenn Verzeichnis leer (im Docker-Image bereits vorhanden)
if [ -z "$(ls -A /app/staticfiles/ 2>/dev/null)" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
else
    echo "Static files already present, skipping collectstatic."
fi

# Wait for database
wait_for_db

# NOTE: Migrationen laufen NICHT im Entrypoint! Zero-Downtime-Workflow:
#   update.sh Phase 2: safemigrate (pre-deploy, vor Container-Swap)
#   update.sh Phase 4: migrate (post-deploy, nach Container-Swap)
# Erststart: install.sh führt migrate nach dem Start separat aus.

# Meilisearch im Hintergrund konfigurieren (blockiert nicht den Start)
(python manage.py setup_meilisearch 2>&1 || echo "Meilisearch setup skipped (not available)") &

# Start gunicorn mit --preload für schnelleren Worker-Start (~2-3s gespart)
echo "Starting gunicorn..."
exec gunicorn \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 2 \
    --timeout 120 \
    --preload \
    --access-logfile - \
    --error-logfile - \
    mandari.wsgi:application
