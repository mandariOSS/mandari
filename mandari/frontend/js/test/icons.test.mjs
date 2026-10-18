/**
 * Jeder feste Icon-Name in den Templates muss im installierten Lucide-Paket vorkommen (Issue #645).
 *
 * Lucide hat mit 1.0 die Marken-Icons entfernt; `data-lucide="github"` blieb danach still leer. Geprüft
 * werden `data-lucide="name"` und `<c-ui.icon name="name">` mit festem Namen (ohne Template-Tags).
 *
 * Ausführen mit: npm run test:icons
 */

import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { icons } from 'lucide'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const templates = join(projectRoot, 'templates')

const pascal = (name) =>
  name
    .split('-')
    .map((teil) => teil.charAt(0).toUpperCase() + teil.slice(1))
    .join('')

// Gebundene Attribute (`:data-lucide=`, `x-bind:data-lucide=`) enthalten Ausdrücke, keine Namen
const MUSTER = [/(?<![:.\w-])data-lucide="([a-z0-9-]+)"/g, /<c-ui\.icon\b[^>]*?\sname="([a-z0-9-]+)"/g]

function htmlDateien(verzeichnis, ergebnis = []) {
  for (const eintrag of readdirSync(verzeichnis, { withFileTypes: true })) {
    const pfad = join(verzeichnis, eintrag.name)
    if (eintrag.isDirectory()) htmlDateien(pfad, ergebnis)
    else if (eintrag.name.endsWith('.html')) ergebnis.push(pfad)
  }
  return ergebnis
}

const fehlend = []
let geprueft = 0
for (const datei of htmlDateien(templates)) {
  const text = readFileSync(datei, 'utf8')
  for (const muster of MUSTER) {
    for (const treffer of text.matchAll(muster)) {
      geprueft += 1
      if (!(pascal(treffer[1]) in icons)) fehlend.push(`${relative(projectRoot, datei)}: ${treffer[1]}`)
    }
  }
}

assert.ok(geprueft > 100, `Zu wenige Icon-Namen gefunden (${geprueft}) – Muster noch passend?`)
assert.deepEqual(fehlend, [], `Icon-Namen ohne Lucide-Icon:\n${fehlend.join('\n')}`)
console.log(`✓ ${geprueft} feste Icon-Namen in Templates, alle im Lucide-Paket vorhanden`)
