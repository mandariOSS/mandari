#!/bin/bash
# =============================================================================
# Mandari - Sync to Public Repository
# =============================================================================
# Synchronizes the private dev repository to the public community repository.
# Excludes enterprise-only files and sensitive configurations.
#
# Usage:
#   ./sync-to-public.sh                    # Sync to ../mandari
#   ./sync-to-public.sh /path/to/public    # Sync to custom path
#   ./sync-to-public.sh --dry-run          # Show what would be synced
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Default public repo location (sibling directory)
PUBLIC_REPO="${1:-../mandari}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# =============================================================================
# Helper Functions
# =============================================================================
log() {
    echo -e "${GREEN}[SYNC]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# =============================================================================
# Parse Arguments
# =============================================================================
DRY_RUN=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS] [PUBLIC_REPO_PATH]"
            echo ""
            echo "Syncs the private dev repository to the public community repository."
            echo ""
            echo "Options:"
            echo "  --dry-run, -n    Show what would be synced without making changes"
            echo "  --help, -h       Show this help message"
            echo ""
            echo "Arguments:"
            echo "  PUBLIC_REPO_PATH Path to the public repository (default: ../mandari)"
            exit 0
            ;;
        *)
            PUBLIC_REPO="$1"
            shift
            ;;
    esac
done

# =============================================================================
# Pre-flight Checks
# =============================================================================
log "Mandari Sync to Public Repository"
echo "============================================"
echo ""
echo "  Source:  $SCRIPT_DIR"
echo "  Target:  $PUBLIC_REPO"
echo ""

# Check if public repo exists
if [ ! -d "$PUBLIC_REPO" ]; then
    error "Public repository not found: $PUBLIC_REPO"
    echo ""
    echo "Please clone the public repository first:"
    echo "  git clone git@github.com:mandariOSS/mandari.git $PUBLIC_REPO"
    exit 1
fi

# Check if public repo is a git repository
if [ ! -d "$PUBLIC_REPO/.git" ]; then
    error "$PUBLIC_REPO is not a git repository"
fi

# =============================================================================
# Sync Configuration
# =============================================================================
# Files and directories to EXCLUDE from public repo
EXCLUDE_PATTERNS=(
    # Enterprise deployment
    ".enterprise"
    "infrastructure"

    # Private GitHub workflows
    ".github/workflows/deploy.yml"

    # Environment files
    ".env"
    ".env.*"
    "!.env.example"

    # AI context files (keep in private repo only)
    "CLAUDE.md"
    "AGENTS.md"

    # Python artifacts
    "*.pyc"
    "__pycache__"
    ".venv"
    "venv"

    # Node artifacts
    "node_modules"

    # IDE
    ".idea"
    ".vscode"

    # OS
    ".DS_Store"
    "Thumbs.db"

    # Build artifacts
    "staticfiles"
    "media"
    "logs"
    "backups"
    "*.log"

    # Data
    "data"
    "*.sql"
    "*.dump"

    # Obsidian (if used for notes)
    ".obsidian"
)

# Build rsync exclude arguments
EXCLUDE_ARGS=""
for pattern in "${EXCLUDE_PATTERNS[@]}"; do
    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude='$pattern'"
done

# =============================================================================
# Perform Sync
# =============================================================================
if [ "$DRY_RUN" = true ]; then
    log "Dry run mode - no changes will be made"
    RSYNC_FLAGS="-avnc --delete"
else
    RSYNC_FLAGS="-av --delete"
fi

log "Starting sync..."
echo ""

# Build and execute rsync command
RSYNC_CMD="rsync $RSYNC_FLAGS \
    --exclude='.git' \
    --exclude='.enterprise' \
    --exclude='infrastructure' \
    --exclude='.github/workflows/deploy.yml' \
    --exclude='.env' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='node_modules' \
    --exclude='.idea' \
    --exclude='.vscode' \
    --exclude='.DS_Store' \
    --exclude='Thumbs.db' \
    --exclude='staticfiles' \
    --exclude='media' \
    --exclude='logs' \
    --exclude='backups' \
    --exclude='*.log' \
    --exclude='data' \
    --exclude='*.sql' \
    --exclude='*.dump' \
    --exclude='.obsidian' \
    --exclude='CLAUDE.md' \
    --exclude='AGENTS.md' \
    --exclude='sync-to-public.sh' \
    ./ '$PUBLIC_REPO/'"

eval "$RSYNC_CMD"

# =============================================================================
# Post-Sync
# =============================================================================
if [ "$DRY_RUN" = true ]; then
    echo ""
    log "Dry run complete. No changes were made."
    echo ""
    echo "To perform the actual sync, run:"
    echo "  $0"
else
    echo ""
    log "Sync completed successfully!"
    echo ""
    echo "Next steps:"
    echo "  cd $PUBLIC_REPO"
    echo "  git status"
    echo "  git add -A"
    echo "  git commit -m 'Sync from dev repository'"
    echo "  git push origin main"
fi

# =============================================================================
# Verification
# =============================================================================
if [ "$DRY_RUN" = false ]; then
    echo ""
    log "Verifying no sensitive files were synced..."

    SENSITIVE_FILES=(
        ".enterprise"
        "infrastructure/ansible"
        "infrastructure/terraform"
        ".github/workflows/deploy.yml"
        "CLAUDE.md"
        "AGENTS.md"
    )

    LEAKED=false
    for file in "${SENSITIVE_FILES[@]}"; do
        if [ -e "$PUBLIC_REPO/$file" ]; then
            warn "Sensitive file found: $file"
            LEAKED=true
        fi
    done

    if [ "$LEAKED" = true ]; then
        error "Sensitive files were synced! Please review and clean up."
    else
        log "Verification passed - no sensitive files in public repo"
    fi
fi
