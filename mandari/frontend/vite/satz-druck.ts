import { resolve } from 'node:path'
import { build } from 'esbuild'
import type { Plugin } from 'vite'

/**
 * Druckansicht der Satz-Kette als ein klassisches Skript (Issue #1584).
 *
 * Die PDF-Erzeugung (headless Chromium, apps/common/pdf_chromium.py) lädt die Druckansicht aus Dateien. Von dort lädt
 * Chromium keine ES-Module (Herkunft „null“); der Vite-Build mit seinen geteilten Chunks taugt dafür nicht. Nach dem
 * Vite-Build bündelt esbuild deshalb frontend/satz/druck.ts samt TipTap zu static/dist/satz-druck.js (IIFE, ohne
 * pdf.js: Das Briefpapier legt der Server unter). apps/work/motions/druck.py liest die Datei von dort.
 */
export function satzDruck(root: string): Plugin {
  return {
    name: 'mandari-satz-druck',
    apply: 'build',
    async closeBundle() {
      await build({
        entryPoints: [resolve(root, 'frontend/satz/druck.ts')],
        outfile: resolve(root, 'static/dist/satz-druck.js'),
        bundle: true,
        format: 'iife',
        minify: true,
        target: 'es2020',
        legalComments: 'none',
        logLevel: 'warning',
      })
    },
  }
}
