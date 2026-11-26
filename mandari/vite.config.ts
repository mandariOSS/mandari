import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import { lucideIcons } from './frontend/vite/lucide-icons'
import { satzDruck } from './frontend/vite/satz-druck'

/**
 * Vite-Build für mandari (django-vite liest das Manifest).
 *
 * Einstiege:
 *   frontend/js/main.ts       – alle Layouts (HTMX, Alpine, Icons, Toasts)
 *   frontend/js/work.ts       – Work-Portal (seitenbezogene Alpine-Komponenten)
 *   frontend/js/webauthn.ts   – Sicherheitsschlüssel/Passkeys (Kontoseite, zweiter Anmeldeschritt)
 *   frontend/editor/index.ts  – nur Editor-Seiten (TipTap, Yjs) inkl. der Alpine-Komponenten
 *                               frontend/alpine/document-editor.ts und prepare-meeting.ts
 *
 * Dazu baut satzDruck (frontend/vite/satz-druck.ts) nach dem Vite-Build die Druckansicht der Satz-Kette als ein
 * klassisches Skript (static/dist/satz-druck.js): headless Chromium lädt die Seite aus Dateien, dort gehen keine
 * ES-Module (Issue #1584).
 *
 * Lucide-Icons: nur die im Projekt genannten im Haupt-Bundle, der Rest bei Bedarf (frontend/vite/lucide-icons.ts).
 *
 * Entwicklung: `npm run dev` (Dev-Server mit HMR, DJANGO_VITE_DEV_MODE=1)
 * Produktion:  `npm run build` → static/dist/ (Manifest + gehashte Dateien)
 */
export default defineConfig({
  base: '/static/dist/',
  plugins: [lucideIcons(__dirname), satzDruck(__dirname)],
  build: {
    manifest: 'manifest.json',
    outDir: resolve(__dirname, 'static/dist'),
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
    rolldownOptions: {
      // Einstiege bekommen keine zusätzlichen Exporte: Gemeinsam genutzter Code (z. B. der Preload-Helfer für
      // das nachgeladene pdf.js) landet in einem eigenen Chunk statt im Einstieg. Sonst importiert der
      // nachgeladene Chunk den Einstieg über dessen Vite-Namen, während die Seite ihn in Produktion unter dem
      // Namen des Manifest-Storage geladen hat – der Browser führte den Einstieg ein zweites Mal aus.
      // Abgesichert durch apps/common/tests/test_vite_manifest.py.
      preserveEntrySignatures: 'strict',
      input: {
        main: resolve(__dirname, 'frontend/js/main.ts'),
        editor: resolve(__dirname, 'frontend/editor/index.ts'),
        work: resolve(__dirname, 'frontend/js/work.ts'),
        webauthn: resolve(__dirname, 'frontend/js/webauthn.ts'),
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
