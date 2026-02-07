# Mandari Marketing Website (Wagtail CMS)

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=website.settings

WORKDIR /app

# Install system dependencies (Pillow + curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libwebp-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Build stage for Tailwind CSS
FROM base AS css-builder

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY package.json tailwind.config.js ./
RUN npm install

COPY static/css/input.css ./static/css/input.css
COPY templates/ ./templates/

RUN npm run build:css

# Final image
FROM base AS final

# Install Python dependencies (pyproject.toml only for layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code
COPY . .
COPY --from=css-builder /app/static/css/styles.css ./static/css/styles.css

# Create static directory
RUN mkdir -p /app/staticfiles

# Fix line endings and make entrypoint executable
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=30s \
    CMD curl -sf http://localhost:8001/health/ || exit 1

CMD ["/app/docker-entrypoint.sh"]
