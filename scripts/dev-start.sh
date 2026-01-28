#!/bin/bash
# Mandari Development Startup Script (Linux/Mac)
# Usage: ./scripts/dev-start.sh

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Mandari Development Environment ==="
echo ""

# Check Docker
echo "1. Checking Docker..."
if ! docker info > /dev/null 2>&1; then
    echo "   ERROR: Docker is not running. Please start Docker."
    exit 1
fi
echo "   Docker is running."

# Start Infrastructure
echo ""
echo "2. Starting infrastructure (PostgreSQL, Redis, Meilisearch)..."
cd "$PROJECT_ROOT/infrastructure/docker"
docker-compose -f docker-compose.dev.yml up -d
echo "   Infrastructure started."

# Wait for PostgreSQL
echo ""
echo "3. Waiting for PostgreSQL to be ready..."
until docker exec mandari-postgres-dev pg_isready -U postgres > /dev/null 2>&1; do
    sleep 1
done
echo "   PostgreSQL is ready."

# Check .env
echo ""
echo "4. Checking environment configuration..."
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "   Creating .env from .env.example..."
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    echo "   IMPORTANT: Edit .env and add your GROQ_API_KEY!"
fi
echo "   Environment file exists."

echo ""
echo "=== Infrastructure Ready ==="
echo ""
echo "Now start the applications in separate terminals:"
echo ""
echo "  API (Terminal 1):"
echo "    cd apps/api"
echo "    source .venv/bin/activate"
echo "    uvicorn src.main:app --reload"
echo ""
echo "  Frontend (Terminal 2):"
echo "    cd apps/web-public"
echo "    npm run dev"
echo ""
echo "URLs:"
echo "  - Frontend:    http://localhost:5173"
echo "  - API:         http://localhost:8000"
echo "  - API Docs:    http://localhost:8000/docs"
echo "  - Meilisearch: http://localhost:7700"
echo ""
