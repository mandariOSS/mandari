# Mandari Django Application
# Build context: repo root (not ./mandari)
# Dockerfile for staging/production with local assets (DSGVO-konform)

FROM python:3.12-slim

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=mandari.settings

WORKDIR /app

# Install system dependencies including Node.js
# pycairo requires: libcairo2-dev, pkg-config
# pdf2image requires: poppler-utils
# pytesseract requires: tesseract-ocr
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libpq-dev \
    build-essential \
    curl \
    gnupg \
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libffi-dev \
    shared-mime-info \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-deu \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash mandari

# Install shared package first (mandari-oparl)
COPY shared/ /tmp/shared/
RUN pip install --no-cache-dir /tmp/shared/

# Copy requirements first (for caching)
COPY mandari/requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy package.json and tailwind config first (for caching)
COPY mandari/package.json mandari/tailwind.config.js ./

# Install npm dependencies and build Tailwind CSS
RUN npm install && \
    mkdir -p static/css

# Copy application code
COPY --chown=mandari:mandari mandari/ .

# Build Tailwind CSS
RUN npm run build:css

# Create directories
RUN mkdir -p /app/staticfiles /app/media && \
    chown -R mandari:mandari /app

# Fix line endings and make entrypoint executable
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

# Switch to non-root user
USER mandari

# Expose port
EXPOSE 8000

# Health check (longer start period for collectstatic + migrate)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

# Run entrypoint (collectstatic, migrate, then gunicorn)
CMD ["/app/docker-entrypoint.sh"]
