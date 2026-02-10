#!/bin/bash
set -e

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

# Collect static files (to shared volume)
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Wait for database
wait_for_db

# NOTE: Migrations are handled by Ansible on PRIMARY only
# This avoids race conditions and duplicate migration runs
# See: infrastructure/ansible/playbooks/deploy.yml

# Configure Meilisearch indexes (idempotent, includes synonyms)
echo "Configuring Meilisearch..."
python manage.py setup_meilisearch 2>&1 || echo "Meilisearch setup skipped (not available)"

# Start gunicorn
echo "Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 120 mandari.wsgi:application
