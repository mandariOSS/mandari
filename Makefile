# =============================================================================
# Mandari 2.0 - Makefile
# =============================================================================
# Convenience commands for development and deployment
#
# Usage:
#   make help          - Show available commands
#   make deploy        - Deploy to production
#   make deploy-staging - Deploy to staging
# =============================================================================

.PHONY: help install dev test lint format build deploy deploy-staging \
        infra-init infra-plan infra-apply setup backup status logs ssh-master ssh-slave

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
# Infrastructure (Terraform)
# =============================================================================

infra-init: ## Initialize Terraform
	@echo "$(BLUE)Initializing Terraform...$(NC)"
	cd infrastructure/terraform && terraform init -upgrade

infra-plan: ## Plan infrastructure changes
	@echo "$(BLUE)Planning infrastructure changes...$(NC)"
	cd infrastructure/terraform && terraform plan

infra-apply: ## Apply infrastructure changes
	@echo "$(YELLOW)Applying infrastructure changes...$(NC)"
	cd infrastructure/terraform && terraform apply

infra-destroy: ## Destroy infrastructure (DANGEROUS!)
	@echo "$(YELLOW)WARNING: This will destroy all infrastructure!$(NC)"
	@read -p "Are you sure? Type 'yes' to continue: " confirm && [ "$$confirm" = "yes" ]
	cd infrastructure/terraform && terraform destroy

infra-output: ## Show Terraform outputs
	cd infrastructure/terraform && terraform output

# =============================================================================
# Server Setup (Ansible)
# =============================================================================

ansible-deps: ## Install Ansible dependencies
	ansible-galaxy install -r infrastructure/ansible/requirements.yml

setup: ## Configure servers with Ansible
	@echo "$(BLUE)Configuring servers...$(NC)"
	cd infrastructure/ansible && ansible-playbook -i inventory/production.yml playbooks/setup.yml

setup-postgres: ## Setup PostgreSQL replication
	@echo "$(BLUE)Setting up PostgreSQL replication...$(NC)"
	cd infrastructure/ansible && ansible-playbook -i inventory/production.yml playbooks/postgres-setup.yml

# =============================================================================
# Deployment
# =============================================================================

deploy: ## Deploy to production
	@echo "$(BLUE)Deploying to production...$(NC)"
	cd infrastructure/scripts && ./deploy.sh app
	@echo "$(GREEN)Deployment complete!$(NC)"

deploy-full: ## Full deployment (infra + setup + app)
	@echo "$(BLUE)Running full deployment...$(NC)"
	cd infrastructure/scripts && ./deploy.sh all
	@echo "$(GREEN)Full deployment complete!$(NC)"

rollback: ## Rollback to previous deployment
	@echo "$(YELLOW)Rolling back deployment...$(NC)"
	cd infrastructure/ansible && ansible-playbook -i inventory/production.yml playbooks/rollback.yml --extra-vars "backup_file=$(BACKUP)"

# =============================================================================
# Operations
# =============================================================================

status: ## Check deployment status
	cd infrastructure/scripts && ./deploy.sh status

logs: ## Show application logs (follow)
	@echo "$(BLUE)Fetching logs from master...$(NC)"
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker compose -f /opt/mandari/docker-compose.yml logs -f --tail=100"

logs-api: ## Show API logs
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker logs -f mandari-api --tail=100"

logs-ingestor: ## Show Ingestor logs
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker logs -f mandari-ingestor --tail=100"

ssh-master: ## SSH into master server
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip)

ssh-slave: ## SSH into slave server
	ssh root@$$(cd infrastructure/terraform && terraform output -raw slave_ip)

# =============================================================================
# Backup & Restore
# =============================================================================

backup: ## Create full backup
	@echo "$(BLUE)Creating backup...$(NC)"
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"/opt/mandari/scripts/backup.sh full"

backup-db: ## Backup database only
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"/opt/mandari/scripts/backup.sh db"

backup-list: ## List available backups
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"/opt/mandari/scripts/backup.sh list"

# =============================================================================
# Database
# =============================================================================

db-shell: ## Open PostgreSQL shell on master
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker exec -it mandari-postgres psql -U mandari"

db-replication: ## Check PostgreSQL replication status
	cd infrastructure/scripts && ./failover.sh check

# =============================================================================
# Sync & Maintenance
# =============================================================================

sync-full: ## Run full OParl sync
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker exec mandari-ingestor python -m src.main sync --full"

sync-incremental: ## Run incremental OParl sync
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker exec mandari-ingestor python -m src.main sync"

collectstatic: ## Collect static files
	ssh root@$$(cd infrastructure/terraform && terraform output -raw master_ip) \
		"docker exec mandari-api python manage.py collectstatic --noinput"

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
