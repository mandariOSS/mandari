# =============================================================================
# Mandari 2.0 - Public Portal Frontend
# =============================================================================
# This Dockerfile builds the public-facing web portal.
#
# OPTION 1: SvelteKit/Node.js Frontend (default)
# OPTION 2: Comment out and use Django static build
# =============================================================================

# -----------------------------------------------------------------------------
# Build Stage
# -----------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Build the application
ARG PUBLIC_API_URL
ARG PUBLIC_SITE_URL
ENV PUBLIC_API_URL=${PUBLIC_API_URL:-http://api:8000}
ENV PUBLIC_SITE_URL=${PUBLIC_SITE_URL:-https://mandari.de}

RUN npm run build

# -----------------------------------------------------------------------------
# Production Stage
# -----------------------------------------------------------------------------
FROM node:20-alpine AS production

WORKDIR /app

# Create non-root user
RUN addgroup -g 1001 -S nodejs && \
    adduser -S sveltekit -u 1001

# Copy built application
COPY --from=builder --chown=sveltekit:nodejs /app/build ./build
COPY --from=builder --chown=sveltekit:nodejs /app/package*.json ./

# Install production dependencies only
RUN npm ci --production && \
    npm cache clean --force

# Switch to non-root user
USER sveltekit

# Expose port
EXPOSE 3000

# Environment variables
ENV NODE_ENV=production
ENV PORT=3000
ENV HOST=0.0.0.0

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget -q --spider http://localhost:3000 || exit 1

# Start the server
CMD ["node", "build"]
