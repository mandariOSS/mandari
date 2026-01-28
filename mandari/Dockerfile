# Mandari Django Application
# Dockerfile for staging/production with local assets (DSGVO-konform)

FROM python:3.12-slim

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=mandari.settings

WORKDIR /app

# Install system dependencies including Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libpq-dev \
    build-essential \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash mandari

# Copy requirements first (for caching)
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy package.json and tailwind config first (for caching)
COPY package.json tailwind.config.js ./

# Install npm dependencies and build Tailwind CSS
RUN npm install && \
    mkdir -p static/css

# Copy application code
COPY --chown=mandari:mandari . .

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
