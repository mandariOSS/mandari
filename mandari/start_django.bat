@echo off
REM Start Django with Waitress (Windows-compatible WSGI server)
REM This script runs Django in production mode on port 8000

cd /d "%~dp0"

echo ========================================
echo Starting Mandari Django Server
echo ========================================
echo.

REM Load environment variables from .env
for /f "tokens=*" %%a in ('type ..\..\.env ^| findstr /v "^#" ^| findstr /v "^$"') do (
    set %%a
)

REM Collect static files
echo Collecting static files...
python manage.py collectstatic --noinput

echo.
echo Starting Waitress server on http://localhost:8000
echo Press Ctrl+C to stop
echo.

REM Start waitress (0.0.0.0 to allow connections from Caddy)
python -c "from waitress import serve; from mandari.wsgi import application; serve(application, host='0.0.0.0', port=8000, threads=4)"
