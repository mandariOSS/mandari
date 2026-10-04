# Mandari Django Application
# Build context: repo root (not ./mandari)
# Multi-stage build: build-deps bleiben nicht im finalen Image

# uv mit fester Version (Dependabot hebt sie wie die Python-Basis), nur für den Export aus uv.lock
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

# =============================================================================
# Stage 1: Python dependencies bauen
# =============================================================================
FROM python:3.14-slim AS python-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# uv nur im Builder, nicht im fertigen Image
COPY --from=uv /uv /bin/uv

# Python dependencies
# Reproduzierbarer Build aus mandari/uv.lock (wie das Ingestor-Image). Verzeichnisse wie im Repo, damit der
# Pfad ../shared aus [tool.uv.sources] aufgeht.
# --locked: Build bricht ab, wenn uv.lock nicht mehr zu pyproject.toml passt (dann `uv lock` im Ordner mandari).
# Export mit Hashes, pip prüft sie bei der Installation (--require-hashes).
COPY shared/pyproject.toml /build/shared/pyproject.toml
COPY mandari/pyproject.toml mandari/uv.lock /build/mandari/
WORKDIR /build/mandari
RUN uv export --quiet --locked --no-dev --no-emit-project --no-emit-package mandari-oparl \
        --format requirements.txt -o /tmp/requirements.lock \
    && pip install --no-cache-dir --prefix=/install --require-hashes -r /tmp/requirements.lock

# Shared package ohne eigene Abhängigkeitsauflösung (pydantic kommt aus der Lock-Datei)
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir --prefix=/install --no-deps /tmp/shared/

# =============================================================================
# Stage 2: Frontend assets bauen
# =============================================================================
FROM node:26-slim AS node-builder

WORKDIR /build

# npm dependencies (cached layer)
COPY mandari/package.json mandari/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install

# Tailwind + Vite sources
COPY mandari/tailwind.config.js mandari/vite.config.ts mandari/tsconfig.json ./
COPY mandari/static/ ./static/
COPY mandari/templates/ ./templates/
COPY mandari/frontend/ ./frontend/
# Python-Quellen nur für die Icon-Auswahl des Vite-Builds: Icon-Namen aus Views und Modellen
# (frontend/vite/lucide-icons.ts). Fehlen sie, lädt die Seite den vollständigen Icon-Satz nach.
COPY mandari/apps/ ./apps/
COPY mandari/hub/ ./hub/
COPY mandari/insight_core/ ./insight_core/
COPY mandari/insight_ai/ ./insight_ai/
COPY mandari/insight_search/ ./insight_search/
COPY mandari/insight_sync/ ./insight_sync/
COPY mandari/mandari/ ./mandari/

# Build CSS (Tailwind) + JS-Bundles (Vite, Manifest in static/dist/)
# Build-Fehler muessen den Image-Build abbrechen (kein stilles Ausliefern alter Bundles)
RUN npx tailwindcss -i ./static/css/input.css -o ./static/css/styles.css --minify
RUN npx vite build

# =============================================================================
# Stage 3: Finales Runtime-Image (kein build-essential, kein Node.js)
# =============================================================================
FROM python:3.14-slim

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
COPY --from=node-builder /build/static/dist/ ./static/dist/

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
