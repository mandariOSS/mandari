FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (Pillow needs these)
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
COPY marketing/templates/ ./marketing/templates/ 2>/dev/null || true
COPY blog/templates/ ./blog/templates/ 2>/dev/null || true

RUN npm run build:css

# Final image
FROM base AS final

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY . .
COPY --from=css-builder /app/static/css/styles.css ./static/css/styles.css

RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=30s \
    CMD curl -sf http://localhost:8001/health/ || exit 1

CMD ["gunicorn", "website.wsgi:application", "--bind", "0.0.0.0:8001", "--workers", "2", "--timeout", "120"]
