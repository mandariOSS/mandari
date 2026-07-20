# Beispiel-Dockerfile für Aeroid/oparl-bridge (ALLRIS -> OParl-1.1-Proxy)
#
# Upstream (MIT) liefert kein offizielles Image; dieses Dockerfile baut die
# gepinnte Version v0.2.0. Siehe docs/SCRAPER_SOURCES.md.
#
# Build:
#   docker build -f oparl-bridge.Dockerfile \
#     -t mandari/oparl-bridge:v0.2.0 \
#     https://github.com/Aeroid/oparl-bridge.git#v0.2.0

FROM python:3.12-slim

WORKDIR /app

# uv für schnelle, reproduzierbare Installation (uv.lock liegt im Repo)
RUN pip install --no-cache-dir uv

COPY . /app
RUN uv sync --frozen --no-dev

# Playwright-Chromium inkl. Systemabhängigkeiten (Wicket-/Ajax-Scraping)
RUN uv run playwright install --with-deps chromium

# SQLite-Cache auf ein Volume legen
ENV OPARL_DATABASE_URL="sqlite:////data/oparl_bridge.db"
VOLUME /data

EXPOSE 8000

# FastAPI-App (OParl-API + UI); der Scrape-Sync wird über die UI oder
# `uv run oparl-bridge-sync` angestoßen (initial 10-40 min je Kommune).
CMD ["uv", "run", "uvicorn", "oparl_bridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
