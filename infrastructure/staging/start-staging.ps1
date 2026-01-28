# Mandari Staging Environment - Start Script (Windows PowerShell)
# ================================================================
#
# This script sets up and starts the Mandari staging environment.
#
# Prerequisites:
# - Docker Desktop installed and running
# - PowerShell run as Administrator (for hosts file modification)
#

param(
    [switch]$SkipHosts,
    [switch]$Build,
    [switch]$Down,
    [switch]$Logs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Colors for output
function Write-Info { Write-Host "[INFO] $args" -ForegroundColor Cyan }
function Write-Success { Write-Host "[OK] $args" -ForegroundColor Green }
function Write-Warning { Write-Host "[WARN] $args" -ForegroundColor Yellow }
function Write-Error { Write-Host "[ERROR] $args" -ForegroundColor Red }

# Banner
Write-Host ""
Write-Host "  __  __                 _            _ " -ForegroundColor Blue
Write-Host " |  \/  | __ _ _ __   __| | __ _ _ __(_)" -ForegroundColor Blue
Write-Host " | |\/| |/ _`  | '_ \ / _`  |/ _`  | '__| |" -ForegroundColor Blue
Write-Host " | |  | | (_| | | | | (_| | (_| | |  | |" -ForegroundColor Blue
Write-Host " |_|  |_|\__,_|_| |_|\__,_|\__,_|_|  |_|" -ForegroundColor Blue
Write-Host ""
Write-Host " Staging Environment - mandari.dev" -ForegroundColor Gray
Write-Host " ====================================" -ForegroundColor Gray
Write-Host ""

# Change to script directory
Set-Location $ScriptDir

# Handle --down flag
if ($Down) {
    Write-Info "Stopping staging environment..."
    docker-compose -f docker-compose.staging.yml down
    Write-Success "Staging environment stopped."
    exit 0
}

# Handle --logs flag
if ($Logs) {
    Write-Info "Showing logs (Ctrl+C to exit)..."
    docker-compose -f docker-compose.staging.yml logs -f
    exit 0
}

# Step 1: Check Docker
Write-Info "Checking Docker..."
$dockerRunning = docker info 2>$null
if (-not $?) {
    Write-Error "Docker is not running. Please start Docker Desktop."
    exit 1
}
Write-Success "Docker is running."

# Step 2: Add hosts file entry
if (-not $SkipHosts) {
    Write-Info "Checking hosts file entry for mandari.dev..."
    $hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
    $hostsContent = Get-Content $hostsFile -Raw

    if ($hostsContent -notmatch "mandari\.dev") {
        Write-Warning "Adding mandari.dev to hosts file (requires Admin rights)..."

        # Check if running as admin
        $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

        if (-not $isAdmin) {
            Write-Error "Please run this script as Administrator to modify hosts file."
            Write-Info "Or run with -SkipHosts and manually add: 127.0.0.1 mandari.dev"
            exit 1
        }

        Add-Content -Path $hostsFile -Value "`n# Mandari Staging`n127.0.0.1 mandari.dev"
        Write-Success "Added mandari.dev to hosts file."
    } else {
        Write-Success "mandari.dev already in hosts file."
    }
}

# Step 3: Create .env file if not exists
if (-not (Test-Path ".env")) {
    Write-Info "Creating .env from template..."
    Copy-Item ".env.staging" ".env"
    Write-Success ".env file created. Please review and adjust settings."
}

# Step 4: Build images if requested
if ($Build) {
    Write-Info "Building Docker images..."
    docker-compose -f docker-compose.staging.yml build
    Write-Success "Images built successfully."
}

# Step 5: Start containers
Write-Info "Starting staging environment..."
docker-compose -f docker-compose.staging.yml up -d

# Step 6: Wait for services
Write-Info "Waiting for services to be ready..."
$maxRetries = 30
$retry = 0

while ($retry -lt $maxRetries) {
    $healthCheck = docker-compose -f docker-compose.staging.yml exec -T django curl -s http://localhost:8000/health/ 2>$null
    if ($healthCheck -match "ok") {
        break
    }
    $retry++
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}
Write-Host ""

if ($retry -ge $maxRetries) {
    Write-Warning "Services may not be fully ready. Check logs with: .\start-staging.ps1 -Logs"
} else {
    Write-Success "All services are ready!"
}

# Step 7: Show status
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host " Mandari Staging Environment is running!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host " Web Application:  http://mandari.dev" -ForegroundColor White
Write-Host " Django Admin:     http://mandari.dev/admin/" -ForegroundColor White
Write-Host " pgAdmin:          http://localhost:5050 (use --profile tools)" -ForegroundColor Gray
Write-Host ""
Write-Host " Commands:" -ForegroundColor Yellow
Write-Host "   Stop:    .\start-staging.ps1 -Down" -ForegroundColor Gray
Write-Host "   Logs:    .\start-staging.ps1 -Logs" -ForegroundColor Gray
Write-Host "   Rebuild: .\start-staging.ps1 -Build" -ForegroundColor Gray
Write-Host ""
