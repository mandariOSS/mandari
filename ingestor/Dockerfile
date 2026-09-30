# Mandari Ingestor - Dockerfile
# Build context: repo root (not ./ingestor)
# Optimized for faster builds with better caching

# uv mit fester Version (Dependabot hebt sie wie die Python-Basis)
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM python:3.14-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv nur im Builder, nicht im fertigen Image
COPY --from=uv /uv /bin/uv

# Abhängigkeiten reproduzierbar aus ingestor/uv.lock, wie das Django-Image aus mandari/uv.lock.
# Ohne Lock-Datei zog ein Neubau ungeprüfte neue Versionen (SQLAlchemy 2.1 ohne greenlet: Ingestor
# startete nicht). Verzeichnisse wie im Repo, damit der Pfad ../shared aus [tool.uv.sources] aufgeht.
# --locked: Build bricht ab, wenn uv.lock nicht mehr zu pyproject.toml passt (dann `uv lock` im Ordner ingestor).
# Export mit Hashes, die Installation prüft sie (--require-hashes).
COPY shared/pyproject.toml /build/shared/pyproject.toml
COPY ingestor/pyproject.toml ingestor/uv.lock ingestor/README.md /build/ingestor/
WORKDIR /build/ingestor
RUN uv export --quiet --locked --no-dev --no-emit-project --no-emit-package mandari-oparl \
        --format requirements.txt -o /tmp/requirements.lock \
    && uv pip install --system --require-hashes -r /tmp/requirements.lock

# Lokale Pakete ohne eigene Abhängigkeitsauflösung (alle Abhängigkeiten kommen aus der Lock-Datei).
# --no-sources: [tool.uv.sources] (lokales ../shared) gilt nur für uv lock/sync.
COPY shared/ /build/shared/
RUN uv pip install --system --no-deps /build/shared/ \
    && uv pip install --system --no-deps --no-sources /build/ingestor/

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

# Build-Pruefung: Abhaengigkeiten vollstaendig und jedes Modul unter src/ importierbar
# (ohne Datenbank und Netz; kein Modul baut beim Import eine Verbindung auf).
# Ein fehlendes Paket laesst so den Build scheitern statt den Start in Produktion.
# greenlet ausdruecklich: Es kommt ab SQLAlchemy 2.1 nur noch ueber das Extra [asyncio]. Unter 2.0 liess sich
# sqlalchemy.ext.asyncio auch ohne greenlet importieren und scheiterte erst beim ersten Datenbankzugriff;
# die eigene Zeile haelt die Pruefung von der SQLAlchemy-Version unabhaengig.
RUN python -m pip check \
    && python -B -c "import importlib, pkgutil, src; import greenlet, sqlalchemy.ext.asyncio; \
[importlib.import_module(m.name) for m in pkgutil.walk_packages(src.__path__, 'src.') if not m.name.endswith('__main__')]"

# Create data directory and non-root user
RUN mkdir -p /app/data && \
    useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Default command (can be overridden)
CMD ["python", "-m", "src.main", "status"]
