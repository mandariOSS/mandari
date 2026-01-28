@echo off
REM Start Mandari Server (Django + Caddy)
REM Run this as Administrator for port 80/443 access

cd /d "%~dp0"

echo ========================================
echo    MANDARI SERVER - volt.mandari.de
echo ========================================
echo.
echo Starting services...
echo.

REM Start Django in background
start "Mandari Django" cmd /c start_django.bat

REM Wait for Django to start
echo Waiting for Django to start...
timeout /t 5 /nobreak > nul

REM Start Caddy (needs Admin for ports 80/443)
echo Starting Caddy (SSL)...
echo.
echo NOTE: Run as Administrator if you get permission errors!
echo.

call start_caddy.bat
