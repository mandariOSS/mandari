# Mandari Development Startup Script (Windows PowerShell)
# Usage: .\scripts\dev-start.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "=== Mandari Development Environment ===" -ForegroundColor Cyan
Write-Host ""

# Check Docker
Write-Host "1. Checking Docker..." -ForegroundColor Yellow
$dockerRunning = docker info 2>$null
if (-not $?) {
    Write-Host "   ERROR: Docker is not running. Please start Docker Desktop." -ForegroundColor Red
    exit 1
}
Write-Host "   Docker is running." -ForegroundColor Green

# Start Infrastructure
Write-Host ""
Write-Host "2. Starting infrastructure (PostgreSQL, Redis, Meilisearch)..." -ForegroundColor Yellow
Set-Location "$ProjectRoot\infrastructure\docker"
docker-compose -f docker-compose.dev.yml up -d
Write-Host "   Infrastructure started." -ForegroundColor Green

# Wait for PostgreSQL
Write-Host ""
Write-Host "3. Waiting for PostgreSQL to be ready..." -ForegroundColor Yellow
$retries = 0
while ($retries -lt 30) {
    $result = docker exec mandari-postgres-dev pg_isready -U postgres 2>$null
    if ($?) {
        Write-Host "   PostgreSQL is ready." -ForegroundColor Green
        break
    }
    Start-Sleep -Seconds 1
    $retries++
}

# Check .env
Write-Host ""
Write-Host "4. Checking environment configuration..." -ForegroundColor Yellow
if (-not (Test-Path "$ProjectRoot\.env")) {
    Write-Host "   Creating .env from .env.example..." -ForegroundColor Yellow
    Copy-Item "$ProjectRoot\.env.example" "$ProjectRoot\.env"
    Write-Host "   IMPORTANT: Edit .env and add your GROQ_API_KEY!" -ForegroundColor Magenta
}
Write-Host "   Environment file exists." -ForegroundColor Green

Write-Host ""
Write-Host "=== Infrastructure Ready ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Now start the applications in separate terminals:" -ForegroundColor White
Write-Host ""
Write-Host "  API (Terminal 1):" -ForegroundColor Yellow
Write-Host "    cd apps\api"
Write-Host "    .venv\Scripts\activate"
Write-Host "    uvicorn src.main:app --reload"
Write-Host ""
Write-Host "  Frontend (Terminal 2):" -ForegroundColor Yellow
Write-Host "    cd apps\web-public"
Write-Host "    npm run dev"
Write-Host ""
Write-Host "URLs:" -ForegroundColor White
Write-Host "  - Frontend:    http://localhost:5173"
Write-Host "  - API:         http://localhost:8000"
Write-Host "  - API Docs:    http://localhost:8000/docs"
Write-Host "  - Meilisearch: http://localhost:7700"
Write-Host "  - pgAdmin:     docker-compose --profile tools up pgadmin"
Write-Host ""
