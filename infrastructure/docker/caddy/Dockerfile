# =============================================================================
# Custom Caddy with Hetzner DNS Plugin
# =============================================================================
# This Dockerfile builds Caddy with the Hetzner DNS provider module
# for DNS-01 ACME challenges (required for multi-server setups behind LB)
# =============================================================================

FROM caddy:2-builder AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/hetzner

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
