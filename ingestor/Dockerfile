# Mandari Ingestor - Dockerfile
# Build context: repo root (not ./ingestor)
# Optimized for faster builds with better caching

# uv mit fester Version (Dependabot hebt sie wie die Python-Basis)
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.14-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv nur im Builder, nicht im fertigen Image. UV_NO_BUILD: uv installiert nur fertige Wheels, ein Bau lüde das
# Build-Backend ohne Hash-Prüfung aus dem Paketindex (scripts/check_image_hashes.py verlangt das).
COPY --from=uv /uv /bin/uv
ENV UV_NO_BUILD=1

# Abhängigkeiten reproduzierbar aus ingestor/uv.lock, wie das Django-Image aus mandari/uv.lock.
# Ohne Lock-Datei zog ein Neubau ungeprüfte neue Versionen (SQLAlchemy 2.1 ohne greenlet: Ingestor
# startete nicht). Verzeichnisse wie im Repo, damit der Pfad ../shared aus [tool.uv.sources] aufgeht.
# --locked: Build bricht ab, wenn uv.lock nicht mehr zu pyproject.toml passt (dann `uv lock` im Ordner ingestor).
# --offline: Der Export liest nur uv.lock und die pyproject.toml-Dateien. Ein lokales Paket, dessen Metadaten nicht
# feststehen, baut uv dafür auch mit UV_NO_BUILD; ohne Netz scheitert das, statt ein Build-Backend zu laden.
# Export mit Hashes, die Installation prüft sie (--require-hashes). --no-build lässt nur fertige Wheels zu: Ein
# Quelltextpaket bräuchte zum Bauen ein Build-Backend, das uv ohne Hash-Prüfung nachladen würde.
# scripts/check_image_hashes.py prüft diese Regeln.
COPY shared/pyproject.toml /build/shared/pyproject.toml
COPY ingestor/pyproject.toml ingestor/uv.lock ingestor/README.md /build/ingestor/
WORKDIR /build/ingestor
RUN uv export --quiet --locked --offline --no-dev --no-emit-project --no-emit-package mandari-oparl \
        --format requirements.txt -o /tmp/requirements.lock \
    && uv pip install --system --no-build --require-hashes -r /tmp/requirements.lock

# Build-Backend der lokalen Pakete (Gruppe build-backend: hatchling) ebenso mit Hashes aus uv.lock, aber in einer
# eigenen Umgebung: /usr/local geht vollständig ins fertige Image, das Build-Backend nicht.
RUN uv export --quiet --locked --offline --only-group build-backend --no-emit-project \
        --format requirements.txt -o /tmp/build-backend.lock \
    && python -m venv /opt/build-backend \
    && /opt/build-backend/bin/pip install --no-cache-dir --only-binary :all: --require-hashes \
        -r /tmp/build-backend.lock

# Lokale Pakete ohne eigene Abhängigkeitsauflösung (alle Abhängigkeiten kommen aus der Lock-Datei) und ohne Netz:
# Wheels mit dem Build-Backend von oben bauen (--no-build-isolation), dann ins System installieren.
# [tool.uv.sources] (lokales ../shared) gilt nur für uv lock/sync und spielt hier keine Rolle.
COPY shared/ /build/shared/
RUN --network=none /opt/build-backend/bin/pip wheel --no-cache-dir --no-deps --no-build-isolation --no-index \
        --wheel-dir /tmp/wheels /build/shared/ /build/ingestor/ \
    && uv pip install --system --no-deps --no-index /tmp/wheels/*.whl

# --- Final stage ---
FROM python:3.14-slim

WORKDIR /app

# Copy installed packages from builder
# Der Pfad traegt die Python-Version und muss mit der FROM-Zeile wandern.
# Dependabot bumpt nur FROM -- genau daran scheiterte der Build fuer 3.14 (#255).
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Install system packages for OCR
# Vorher die Sicherheitsupdates von Debian einspielen: Das Basisimage wird nur gelegentlich neu gebaut,
# Korrekturen (etwa für perl-base) kämen sonst erst mit dem nächsten Basisimage ins Laufzeitimage.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY ingestor/src/ src/

# Build-Prüfung: Abhängigkeiten vollständig und jedes Modul unter src/ importierbar
# (ohne Datenbank und Netz; kein Modul baut beim Import eine Verbindung auf).
# Ein fehlendes Paket lässt so den Build scheitern statt den Start in Produktion.
# greenlet ausdrücklich: Es kommt ab SQLAlchemy 2.1 nur noch über das Extra [asyncio]. Unter 2.0 ließ sich
# sqlalchemy.ext.asyncio auch ohne greenlet importieren und scheiterte erst beim ersten Datenbankzugriff;
# die eigene Zeile hält die Prüfung von der SQLAlchemy-Version unabhängig.
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
