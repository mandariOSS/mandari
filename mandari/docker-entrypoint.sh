#!/bin/bash
set -e

# =============================================================================
# Mandari - Docker Entrypoint
# =============================================================================
# Optimiert für schnellen Startup (Zero-Downtime Updates):
#   - collectstatic nur wenn nötig
#   - Meilisearch-Setup im Hintergrund
#   - Daphne (ASGI) für HTTP + WebSocket (Echtzeit-Collaboration)
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

# Start Daphne (ASGI) — HTTP + WebSocket auf demselben Port
# Daphne ist der offizielle ASGI-Server von Django Channels und unterstützt
# sowohl HTTP als auch WebSocket-Verbindungen (nötig für Echtzeit-Collaboration).
# Gunicorn (WSGI) kann KEINE WebSocket-Verbindungen handhaben.
echo "Starting daphne (ASGI)..."
exec daphne \
    --bind 0.0.0.0 \
    --port 8000 \
    --verbosity 1 \
    --access-log - \
    --proxy-headers \
    mandari.asgi:application
