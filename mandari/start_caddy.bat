@echo off
REM Start Caddy with SSL for volt.mandari.de
cd /d "%~dp0"

echo ========================================
echo Starting Caddy (SSL Reverse Proxy)
echo Domain: volt.mandari.de
echo ========================================

REM Caddy path (installed via winget)
set CADDY_PATH=C:\Users\Sven\AppData\Local\Microsoft\WinGet\Links\caddy.exe

"%CADDY_PATH%" run --config Caddyfile
