# Mandari Ingestor - Dockerfile
# Build context: repo root (not ./ingestor)
# Optimized for faster builds with better caching

FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
RUN pip install --no-cache-dir uv

# Install shared package first (mandari-oparl)
COPY shared/ /tmp/shared/
RUN uv pip install --system /tmp/shared/

# Copy only dependency files (for caching)
COPY ingestor/pyproject.toml ingestor/README.md ./

# Install dependencies (without editable mode for faster install)
RUN uv pip install --system .

# --- Final stage ---
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY ingestor/src/ src/

# Create data directory and non-root user
RUN mkdir -p /app/data && \
    useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Default command (can be overridden)
CMD ["python", "-m", "src.main", "status"]
