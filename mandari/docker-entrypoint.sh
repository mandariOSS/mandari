#!/bin/bash
set -e

# Collect static files (to shared volume)
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Run migrations if needed
echo "Running database migrations..."
python manage.py migrate --noinput

# Start gunicorn
echo "Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 120 mandari.wsgi:application
