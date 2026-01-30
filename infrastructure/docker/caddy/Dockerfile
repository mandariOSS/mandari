# =============================================================================
# Custom Caddy with Hetzner DNS Plugin (v2 for Cloud DNS API)
# =============================================================================
# This Dockerfile builds Caddy with the Hetzner DNS provider module
# for DNS-01 ACME challenges (required for multi-server setups behind LB)
#
# IMPORTANT: Using v2 of the plugin for the NEW Hetzner Console Cloud DNS API
# (dns.hetzner.com was migrated to console.hetzner.cloud)
# =============================================================================

FROM caddy:2-builder AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/hetzner/v2

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
