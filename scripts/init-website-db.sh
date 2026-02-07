#!/bin/bash
# Create the marketing website database if it doesn't exist
set -e

WEBSITE_DB="${WEBSITE_DB:-mandari_website}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE $WEBSITE_DB'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$WEBSITE_DB')\gexec
    GRANT ALL PRIVILEGES ON DATABASE $WEBSITE_DB TO $POSTGRES_USER;
EOSQL
