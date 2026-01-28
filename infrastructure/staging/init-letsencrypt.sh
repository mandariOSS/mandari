#!/bin/bash

# Mandari Let's Encrypt SSL Certificate Initialization
# Based on: https://github.com/wmnnd/nginx-certbot

set -e

# Configuration
domains=(mandari.dev www.mandari.dev)
rsa_key_size=4096
data_path="./certbot"
email="admin@mandari.dev"  # Adding a valid address is strongly recommended
staging=0  # Set to 1 if you're testing your setup to avoid hitting request limits

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Mandari Let's Encrypt SSL Setup ===${NC}"
echo ""

# Check if docker-compose is available
if ! [ -x "$(command -v docker-compose)" ] && ! [ -x "$(command -v docker)" ]; then
  echo -e "${RED}Error: docker-compose is not installed.${NC}" >&2
  exit 1
fi

# Use docker compose v2 if available
if docker compose version > /dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
else
  DOCKER_COMPOSE="docker-compose"
fi

# Create required directories
echo -e "${YELLOW}Creating directories...${NC}"
mkdir -p "$data_path/conf"
mkdir -p "$data_path/www"

# Download recommended TLS parameters
if [ ! -e "$data_path/conf/options-ssl-nginx.conf" ] || [ ! -e "$data_path/conf/ssl-dhparams.pem" ]; then
  echo -e "${YELLOW}Downloading recommended TLS parameters...${NC}"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf > "$data_path/conf/options-ssl-nginx.conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem > "$data_path/conf/ssl-dhparams.pem"
  echo -e "${GREEN}TLS parameters downloaded.${NC}"
fi

# Create dummy certificate for nginx to start
echo -e "${YELLOW}Creating dummy certificate for ${domains[0]}...${NC}"
path="/etc/letsencrypt/live/${domains[0]}"
mkdir -p "$data_path/conf/live/${domains[0]}"

$DOCKER_COMPOSE -f docker-compose.staging.yml run --rm --entrypoint "\
  openssl req -x509 -nodes -newkey rsa:$rsa_key_size -days 1\
    -keyout '$path/privkey.pem' \
    -out '$path/fullchain.pem' \
    -subj '/CN=localhost'" certbot
echo -e "${GREEN}Dummy certificate created.${NC}"

# Start nginx
echo -e "${YELLOW}Starting nginx...${NC}"
$DOCKER_COMPOSE -f docker-compose.staging.yml up --force-recreate -d nginx
echo -e "${GREEN}Nginx started.${NC}"

# Wait for nginx to be ready
echo -e "${YELLOW}Waiting for nginx to be ready...${NC}"
sleep 5

# Delete dummy certificate
echo -e "${YELLOW}Deleting dummy certificate for ${domains[0]}...${NC}"
$DOCKER_COMPOSE -f docker-compose.staging.yml run --rm --entrypoint "\
  rm -Rf /etc/letsencrypt/live/${domains[0]} && \
  rm -Rf /etc/letsencrypt/archive/${domains[0]} && \
  rm -Rf /etc/letsencrypt/renewal/${domains[0]}.conf" certbot
echo -e "${GREEN}Dummy certificate deleted.${NC}"

# Request Let's Encrypt certificate
echo -e "${YELLOW}Requesting Let's Encrypt certificate for ${domains[*]}...${NC}"

# Join domains with -d flag
domain_args=""
for domain in "${domains[@]}"; do
  domain_args="$domain_args -d $domain"
done

# Select appropriate email arg
case "$email" in
  "") email_arg="--register-unsafely-without-email" ;;
  *) email_arg="--email $email" ;;
esac

# Enable staging mode if needed
if [ $staging != "0" ]; then
  staging_arg="--staging"
  echo -e "${YELLOW}Using Let's Encrypt staging environment (certificates won't be valid)${NC}"
else
  staging_arg=""
fi

$DOCKER_COMPOSE -f docker-compose.staging.yml run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    $staging_arg \
    $email_arg \
    $domain_args \
    --rsa-key-size $rsa_key_size \
    --agree-tos \
    --force-renewal" certbot

echo -e "${GREEN}=== Certificate obtained successfully! ===${NC}"

# Reload nginx
echo -e "${YELLOW}Reloading nginx...${NC}"
$DOCKER_COMPOSE -f docker-compose.staging.yml exec nginx nginx -s reload

echo ""
echo -e "${GREEN}=== Setup complete! ===${NC}"
echo -e "${GREEN}Your site is now available at https://${domains[0]}${NC}"
echo ""
echo -e "To start all services: ${YELLOW}$DOCKER_COMPOSE -f docker-compose.staging.yml up -d${NC}"
