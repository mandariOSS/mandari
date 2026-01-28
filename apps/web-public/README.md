# Mandari Public Portal

This directory contains the public-facing web portal for Mandari.

## Setup

If using a separate frontend (SvelteKit recommended):

1. Initialize a SvelteKit project:
   ```bash
   npm create svelte@latest .
   ```

2. Configure for server-side rendering with node adapter:
   ```bash
   npm install @sveltejs/adapter-node
   ```

3. Update `svelte.config.js`:
   ```js
   import adapter from '@sveltejs/adapter-node';

   export default {
     kit: {
       adapter: adapter()
     }
   };
   ```

4. Build:
   ```bash
   npm run build
   ```

## Alternative: Use Django Templates

If the public portal is served by Django templates directly, update the Docker Compose to use the Django container instead:

```yaml
web-public:
  image: ${DOCKER_REGISTRY:-ghcr.io/mandari}/mandari:${IMAGE_TAG:-latest}
  # ... same as api service
```

## Environment Variables

- `PUBLIC_API_URL` - Internal API URL (default: `http://api:8000`)
- `PUBLIC_SITE_URL` - Public site URL (default: `https://mandari.de`)
