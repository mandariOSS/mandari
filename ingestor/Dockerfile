# Mandari Ingestor - Dockerfile
# Build context: repo root (not ./ingestor)
# Optimized for faster builds with better caching

FROM python:3.14-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
RUN pip install --no-cache-dir uv

# Copy only dependency files (for caching)
COPY ingestor/pyproject.toml ingestor/uv.lock ingestor/README.md ./

# Abhängigkeiten reproduzierbar aus dem Lockfile – dieselben Versionen, die die CI testet
# (uv sync --frozen). Früher löste `uv pip install .` die Untergrenzen aus pyproject.toml
# bei jedem Build gegen den neuesten PyPI-Stand auf; so kam SQLAlchemy 2.1 ohne greenlet
# ins Image und der Ingestor startete nicht mehr (#636).
# mandari-oparl stammt aus ../shared ([tool.uv.sources]) und wird unten direkt installiert.
RUN uv export --frozen --no-emit-project --no-emit-package mandari-oparl --no-hashes \
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

# Shared-Paket (mandari-oparl) und den Ingestor selbst ohne erneute Auflösung installieren;
# --no-sources: [tool.uv.sources] (lokales ../shared für uv lock) hier ignorieren
COPY shared/ /tmp/shared/
RUN uv pip install --system --no-deps /tmp/shared/ \
    && uv pip install --system --no-deps --no-sources .

# --- Final stage ---
FROM python:3.14-slim

WORKDIR /app

# Copy installed packages from builder
# Der Pfad traegt die Python-Version und muss mit der FROM-Zeile wandern.
# Dependabot bumpt nur FROM -- genau daran scheiterte der Build fuer 3.14 (#255).
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Install system packages for OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY ingestor/src/ src/

# Nur startfähige Images veröffentlichen: fehlt eine Laufzeitabhängigkeit, bricht der Build hier ab
RUN python -c "import src.main"

# Create data directory and non-root user
RUN mkdir -p /app/data && \
    useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Default command (can be overridden)
CMD ["python", "-m", "src.main", "status"]
