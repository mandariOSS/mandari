import { resolve } from 'node:path'
import { defineConfig } from 'vite'

/**
 * Vite-Build für mandari (django-vite liest das Manifest).
 *
 * Einstiege:
 *   frontend/js/main.ts       – alle Layouts (HTMX, Alpine, Icons, Toasts)
 *   frontend/editor/index.ts  – nur Editor-Seiten (TipTap, Yjs) inkl. der Alpine-Komponenten
 *                               frontend/alpine/document-editor.ts und prepare-meeting.ts
 *
 * Entwicklung: `npm run dev` (Dev-Server mit HMR, DJANGO_VITE_DEV_MODE=1)
 * Produktion:  `npm run build` → static/dist/ (Manifest + gehashte Dateien)
 */
export default defineConfig({
  base: '/static/dist/',
  build: {
    manifest: 'manifest.json',
    outDir: resolve(__dirname, 'static/dist'),
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'frontend/js/main.ts'),
        editor: resolve(__dirname, 'frontend/editor/index.ts'),
      },
    },
  },
  server: {
    host: 'localhost',
    port: 5173,
    strictPort: true,
    origin: 'http://localhost:5173',
    cors: true,
  },
})
