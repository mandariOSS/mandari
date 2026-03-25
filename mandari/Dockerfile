# Mandari Django Application
# Build context: repo root (not ./mandari)
# Multi-stage build: build-deps bleiben nicht im finalen Image

# =============================================================================
# Stage 1: Python dependencies bauen
# =============================================================================
FROM python:3.12-slim AS python-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Shared package
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir --prefix=/install /tmp/shared/

# Python dependencies
COPY mandari/requirements.txt /tmp/
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# =============================================================================
# Stage 2: Frontend assets bauen
# =============================================================================
FROM node:20-slim AS node-builder

WORKDIR /build

# npm dependencies (cached layer)
COPY mandari/package.json mandari/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install

# Tailwind + esbuild sources
COPY mandari/tailwind.config.js ./
COPY mandari/static/ ./static/
COPY mandari/templates/ ./templates/
COPY mandari/frontend/ ./frontend/

# Build CSS + Editor bundle
RUN npx tailwindcss -i ./static/css/input.css -o ./static/css/styles.css --minify
RUN npx esbuild frontend/editor/index.ts --bundle --outfile=static/js/editor.bundle.js --format=iife --global-name=MandariEditor --minify --target=es2020 2>/dev/null || true

# =============================================================================
# Stage 3: Finales Runtime-Image (kein build-essential, kein Node.js)
# =============================================================================
FROM python:3.12-slim

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=mandari.settings

WORKDIR /app

# Runtime-Dependencies (keine Compiler, kein Node)
# libpq5: PostgreSQL Client-Library
# libcairo2, libpango-1.0-0: PDF-Generierung (xhtml2pdf)
# poppler-utils: pdf2image
# tesseract-ocr: OCR
# shared-mime-info: MIME-Type-Erkennung
# curl: Health-Checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libffi8 \
    shared-mime-info \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-deu \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd --create-home --shell /bin/bash mandari

# Python packages aus Builder-Stage
COPY --from=python-builder /install /usr/local

# Application code
COPY --chown=mandari:mandari mandari/ .

# Ingestor code (für den eingebauten Sync-Daemon)
COPY --chown=mandari:mandari ingestor/ /ingestor/

# Frontend assets aus Node-Builder-Stage (überschreibt Source-Versionen)
COPY --from=node-builder /build/static/css/styles.css ./static/css/styles.css
COPY --from=node-builder /build/static/js/editor.bundle.js ./static/js/editor.bundle.js

# Verzeichnisse + Berechtigungen
RUN mkdir -p /app/staticfiles /app/media && \
    chown -R mandari:mandari /app

# Entrypoint vorbereiten
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

USER mandari

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/health/ || exit 1

CMD ["/app/docker-entrypoint.sh"]
