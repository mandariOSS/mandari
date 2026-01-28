# Mandari Staging - Firewall Setup
# Run as Administrator!

Write-Host "Setting up Windows Firewall rules for Mandari..." -ForegroundColor Green

# Remove old rules if they exist
netsh advfirewall firewall delete rule name="Mandari HTTP" 2>$null
netsh advfirewall firewall delete rule name="Mandari HTTPS" 2>$null

# Add HTTP rule (port 80)
netsh advfirewall firewall add rule name="Mandari HTTP" dir=in action=allow protocol=TCP localport=80
Write-Host "  [OK] Port 80 (HTTP) opened" -ForegroundColor Cyan

# Add HTTPS rule (port 443)
netsh advfirewall firewall add rule name="Mandari HTTPS" dir=in action=allow protocol=TCP localport=443
Write-Host "  [OK] Port 443 (HTTPS) opened" -ForegroundColor Cyan

Write-Host ""
Write-Host "Firewall rules configured successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "IMPORTANT: You also need to configure port forwarding in your router:" -ForegroundColor Yellow
Write-Host "  - Forward port 80 (TCP) to this computer's local IP" -ForegroundColor Yellow
Write-Host "  - Forward port 443 (TCP) to this computer's local IP" -ForegroundColor Yellow
Write-Host ""

# Show local IP
$localIP = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Ethernet*", "Wi-Fi*" -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
if ($localIP) {
    Write-Host "Your local IP address: $localIP" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "After configuring router port forwarding, run:" -ForegroundColor Green
Write-Host "  docker-compose -f docker-compose.staging.yml run --rm certbot certonly --webroot -w /var/www/certbot -d mandari.dev -d www.mandari.dev --agree-tos --email admin@mandari.dev" -ForegroundColor White
