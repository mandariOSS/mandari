# =============================================================================
# mandari - Makefile
# =============================================================================
# Kurzbefehle für die Entwicklung (Einrichtung und Tests: CONTRIBUTING.md)
# Betrieb (Installation, Update, Sicherung): install.sh, update.sh, backup.sh, siehe DEPLOYMENT.md
#
# Aufruf:
#   make help          - verfügbare Befehle anzeigen
# =============================================================================

.PHONY: help install dev test lint format migrate makemigrations shell \
        docker-build docker-up docker-down docker-logs secrets-generate

# Standardziel
.DEFAULT_GOAL := help

# Entwicklungsdienste: Ports nur auf 127.0.0.1 (docker-compose.dev.yml)
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
DEV_SERVICES := postgres redis elasticsearch

# Farben
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m

# =============================================================================
# Hilfe
# =============================================================================

help: ## Diese Hilfe anzeigen
	@echo ""
	@echo "$(BLUE)mandari - verfügbare Befehle$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""

# =============================================================================
# Entwicklung (Befehle in mandari/ über uv run)
# =============================================================================

install: ## Abhängigkeiten installieren (Python mit Entwicklungswerkzeugen, Node)
	@echo "$(BLUE)Python-Abhängigkeiten aus uv.lock...$(NC)"
	cd mandari && uv sync --extra dev
	@echo "$(BLUE)Node-Abhängigkeiten aus package-lock.json...$(NC)"
	cd mandari && npm ci
	@echo "$(GREEN)Abhängigkeiten installiert.$(NC)"

dev: ## Entwicklungsserver starten
	cd mandari && uv run python manage.py runserver

test: ## Tests ausführen
	cd mandari && uv run pytest

lint: ## Ruff prüfen wie die CI (check und format --check)
	cd mandari && uv run ruff check .
	cd mandari && uv run ruff format --check .

format: ## Code formatieren und Ruff-Befunde beheben
	cd mandari && uv run ruff format .
	cd mandari && uv run ruff check --fix .

migrate: ## Datenbankmigrationen ausführen
	cd mandari && uv run python manage.py migrate

makemigrations: ## Neue Migrationen anlegen
	cd mandari && uv run python manage.py makemigrations

shell: ## Django-Shell öffnen
	cd mandari && uv run python manage.py shell

# =============================================================================
# Docker (lokal)
# =============================================================================

docker-build: ## Images lokal bauen (Tag local, für IMAGE_TAG=local)
	@echo "$(BLUE)Images bauen (Kontext: Repository-Wurzel)...$(NC)"
	docker build -f mandari/Dockerfile -t ghcr.io/mandarioss/mandari:local .
	docker build -f ingestor/Dockerfile -t ghcr.io/mandarioss/ingestor:local .
	@echo "$(GREEN)Images gebaut.$(NC)"

docker-up: ## Entwicklungsdienste starten (PostgreSQL, Redis, Elasticsearch)
	$(COMPOSE_DEV) up -d $(DEV_SERVICES)

docker-down: ## Entwicklungsdienste anhalten
	$(COMPOSE_DEV) stop $(DEV_SERVICES)

docker-logs: ## Protokolle der Entwicklungsdienste anzeigen
	$(COMPOSE_DEV) logs -f $(DEV_SERVICES)

# =============================================================================
# Geheimnisse
# =============================================================================

secrets-generate: ## Neue Geheimnisse für die .env erzeugen
	@echo "$(BLUE)Geheimnisse erzeugen...$(NC)"
	@echo ""
	@echo "SECRET_KEY:"
	@python3 -c "import secrets; print(secrets.token_urlsafe(64))"
	@echo ""
	@echo "ENCRYPTION_MASTER_KEY:"
	@python3 -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
	@echo ""
	@echo "POSTGRES_PASSWORD:"
	@python3 -c "import secrets, string; a = string.ascii_letters + string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
	@echo ""
	@echo "REDIS_PASSWORD:"
	@python3 -c "import secrets, string; a = string.ascii_letters + string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
