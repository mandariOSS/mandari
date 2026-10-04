/**
 * Vite-Plugin: Nur die Lucide-Icons ins Haupt-Bundle, die der Code tatsächlich nennt.
 *
 * `import { icons } from 'lucide'` brachte alle gut 2.100 Icons in `main.js` – rund 80 % des Bundles
 * (451 von 564 KB), auf jeder Seite. Das virtuelle Modul `virtual:lucide-icons` enthält stattdessen die
 * Icons, deren Namen in Templates, Python-Code und Frontend-Quellen vorkommen. Gesucht wird großzügig
 * (jeder Text in Anführungszeichen, der ein Icon-Name ist): Ein überzähliges Icon kostet wenige hundert
 * Byte, ein fehlendes nur Ladezeit – `frontend/js/icons.ts` lädt dann den vollständigen Satz nach.
 *
 * Quellen: `templates/**.html` (`data-lucide`, `icon=…`, `{% block …icon… %}`, `default:'…'` und jeder
 * Text in Anführungszeichen), `**.py` außer Tests und Migrationen, `frontend/**.ts`, `static/js/*.js`.
 */

import { type Dirent, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { icons as alleIcons } from 'lucide'
import type { Plugin } from 'vite'
import { toPascalCase } from '../js/icon-name'

const VIRTUELL = 'virtual:lucide-icons'
const AUFGELOEST = `\0${VIRTUELL}`

const AUSGENOMMEN = new Set([
  'node_modules',
  '.venv',
  'venv',
  'migrations',
  'tests',
  'tests_e2e',
  'dist',
  'staticfiles',
  'media',
  'vendor',
])

function dateien(verzeichnis: string, endungen: string[], ergebnis: string[] = []): string[] {
  let eintraege: Dirent[]
  try {
    eintraege = readdirSync(verzeichnis, { withFileTypes: true })
  } catch {
    return ergebnis
  }
  for (const eintrag of eintraege) {
    if (AUSGENOMMEN.has(eintrag.name) || eintrag.name.startsWith('.')) continue
    const pfad = join(verzeichnis, eintrag.name)
    if (eintrag.isDirectory()) dateien(pfad, endungen, ergebnis)
    else if (endungen.some((endung) => eintrag.name.endsWith(endung)) && !eintrag.name.startsWith('test_')) {
      ergebnis.push(pfad)
    }
  }
  return ergebnis
}

const IN_ANFUEHRUNGSZEICHEN = /["'`]([a-z][a-z0-9]*(?:-[a-z0-9]+)*)["'`]/g
const LUCIDE_ATTRIBUT = /data-lucide="([^"]*)"/g
const WORT = /[a-z][a-z0-9]*(?:-[a-z0-9]+)*/g
const ICON_BLOCK = /\{%\s*block\s+\w*icon\w*\s*%\}\s*([a-z0-9-]+)\s*\{%/g

/** Lucide-Schlüssel (PascalCase) aller Icons, deren Namen im Projekt vorkommen. */
export function genutzteIcons(projekt: string): string[] {
  const gefunden = new Set<string>()
  const pruefen = (name: string) => {
    const schluessel = toPascalCase(name)
    if (schluessel in alleIcons) gefunden.add(schluessel)
  }
  for (const datei of dateien(join(projekt, 'templates'), ['.html'])) {
    const text = readFileSync(datei, 'utf8')
    for (const treffer of text.matchAll(LUCIDE_ATTRIBUT)) for (const wort of treffer[1].matchAll(WORT)) pruefen(wort[0])
    for (const treffer of text.matchAll(ICON_BLOCK)) pruefen(treffer[1])
    for (const treffer of text.matchAll(IN_ANFUEHRUNGSZEICHEN)) pruefen(treffer[1])
  }
  const quellen = [
    ...dateien(projekt, ['.py']),
    ...dateien(join(projekt, 'frontend'), ['.ts']),
    ...dateien(join(projekt, 'static', 'js'), ['.js']),
  ]
  for (const datei of quellen) {
    for (const treffer of readFileSync(datei, 'utf8').matchAll(IN_ANFUEHRUNGSZEICHEN)) pruefen(treffer[1])
  }
  return [...gefunden].sort()
}

export function lucideIcons(projekt: string): Plugin {
  return {
    name: 'mandari-lucide-icons',
    resolveId(id) {
      return id === VIRTUELL ? AUFGELOEST : null
    },
    load(id) {
      if (id !== AUFGELOEST) return null
      const namen = genutzteIcons(projekt)
      return `import { ${namen.join(', ')} } from 'lucide'\nexport const icons = { ${namen.join(', ')} }\n`
    },
  }
}
