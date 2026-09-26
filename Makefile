# =============================================================================
# Mandari 2.0 - Makefile
# =============================================================================
# Convenience commands for development
# Betrieb (Installation, Update, Sicherung): install.sh, update.sh, backup.sh, siehe DEPLOYMENT.md
#
# Usage:
#   make help          - Show available commands
# =============================================================================

.PHONY: help install dev test lint format migrate makemigrations shell \
        docker-build docker-up docker-down docker-logs secrets-generate

# Default target
.DEFAULT_GOAL := help

# Colors
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m

# =============================================================================
# Help
# =============================================================================

help: ## Show this help
	@echo ""
	@echo "$(BLUE)Mandari 2.0 - Available Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""

# =============================================================================
# Development
# =============================================================================

install: ## Install all dependencies
	@echo "$(BLUE)Installing Python dependencies...$(NC)"
	cd mandari && pip install -r requirements.txt
	@echo "$(BLUE)Installing Node dependencies...$(NC)"
	cd mandari && npm install
	@echo "$(GREEN)Dependencies installed!$(NC)"

dev: ## Start development server
	cd mandari && python manage.py runserver

test: ## Run tests
	cd mandari && pytest -v

lint: ## Run linters
	cd mandari && ruff check .
	cd mandari && black --check .

format: ## Format code
	cd mandari && black .
	cd mandari && isort .
	cd mandari && ruff check --fix .

migrate: ## Run database migrations
	cd mandari && python manage.py migrate

makemigrations: ## Create new migrations
	cd mandari && python manage.py makemigrations

shell: ## Open Django shell
	cd mandari && python manage.py shell_plus

# =============================================================================
# Docker (Local)
# =============================================================================

docker-build: ## Build Docker images locally
	@echo "$(BLUE)Building Docker images...$(NC)"
	docker build -t mandari-api:local ./mandari
	docker build -t mandari-ingestor:local ./apps/ingestor
	@echo "$(GREEN)Images built!$(NC)"

docker-up: ## Start local Docker environment
	docker compose -f docker-compose.yml up -d

docker-down: ## Stop local Docker environment
	docker compose -f docker-compose.yml down

docker-logs: ## Show Docker logs
	docker compose -f docker-compose.yml logs -f

# =============================================================================
# Secrets
# =============================================================================

secrets-generate: ## Generate new secrets
	@echo "$(BLUE)Generating secrets...$(NC)"
	@echo ""
	@echo "SECRET_KEY:"
	@python -c "import secrets; print(secrets.token_urlsafe(64))"
	@echo ""
	@echo "ENCRYPTION_MASTER_KEY:"
	@python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
	@echo ""
	@echo "MEILISEARCH_KEY:"
	@python -c "import secrets; print(secrets.token_urlsafe(32))"
	@echo ""
	@echo "POSTGRES_PASSWORD:"
	@python -c "import secrets; print(secrets.token_urlsafe(24))"
	@echo ""
	@echo "REPLICATION_PASSWORD:"
	@python -c "import secrets; print(secrets.token_urlsafe(24))"
