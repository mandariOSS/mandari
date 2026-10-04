/**
 * Lucide-Icons: einmalige Initialisierung plus automatische Nachinitialisierung.
 *
 * Templates schreiben `<i data-lucide="name">`. Ein MutationObserver ersetzt neue
 * Platzhalter, egal ob sie per HTMX-Swap, Alpine (`x-for`, `x-if`) oder direkt per DOM
 * eingefügt wurden. Manuelle `lucide.createIcons()`-Aufrufe sind damit überflüssig.
 *
 * Im Bundle stehen nur die Icons, deren Namen im Projekt vorkommen (`virtual:lucide-icons`, siehe
 * frontend/vite/lucide-icons.ts). Taucht ein anderer Name auf (etwa aus Daten zusammengesetzt), steht
 * an seiner Stelle zunächst ein leeres SVG gleicher Größe; der vollständige Satz wird einmal
 * nachgeladen und der Bereich dann neu gezeichnet.
 */

import { icons as genutzteIcons } from 'virtual:lucide-icons'
import { createIcons, type Icons } from 'lucide'
import { toPascalCase } from './icon-name'

const NAME_ATTR = 'data-lucide'

let verfuegbar: Icons = genutzteIcons
let vollstaendig: Promise<void> | null = null
let geladen = false

function alleIconsLaden(): Promise<void> {
  vollstaendig ??= import('./icons-alle').then((modul) => {
    verfuegbar = modul.icons
    geladen = true
  })
  return vollstaendig
}

/** Icons für `root`; unbekannte Namen bekommen bis zum Nachladen ein leeres SVG (kein Sprung im Layout). */
function iconsFuer(root: ParentNode): { icons: Icons; fehlend: boolean } {
  // Nach dem Nachladen wie bisher: Ein Name, den Lucide nicht kennt, erzeugt dessen Warnung.
  if (geladen) return { icons: verfuegbar, fehlend: false }
  let icons = verfuegbar
  for (const element of root.querySelectorAll(`[${NAME_ATTR}]`)) {
    const schluessel = toPascalCase(element.getAttribute(NAME_ATTR) ?? '')
    if (!schluessel || schluessel in icons) continue
    if (icons === verfuegbar) icons = { ...verfuegbar }
    icons[schluessel] = []
  }
  return { icons, fehlend: icons !== verfuegbar }
}

function hasPlaceholder(node: Node): node is Element {
  if (node.nodeType !== Node.ELEMENT_NODE) return false
  const el = node as Element
  return el.hasAttribute(NAME_ATTR) || el.querySelector(`[${NAME_ATTR}]`) !== null
}

/** Ersetzt alle `[data-lucide]`-Platzhalter unterhalb von `root` (Standard: document). */
export function renderIcons(root: ParentNode = document): void {
  const { icons, fehlend } = iconsFuer(root)
  createIcons({ icons, nameAttr: NAME_ATTR, root: root as Element | Document })
  if (fehlend) {
    void alleIconsLaden().then(() => {
      if (!(root instanceof Element) || root.isConnected) renderIcons(root)
    })
  }
}

let scheduled = false
const pending = new Set<Element>()

function flush(): void {
  scheduled = false
  const roots = Array.from(pending)
  pending.clear()
  for (const root of roots) {
    if (root.isConnected) renderIcons(root)
  }
}

function schedule(el: Element): void {
  pending.add(el)
  if (!scheduled) {
    scheduled = true
    requestAnimationFrame(flush)
  }
}

/** Startet die Erstinitialisierung und beobachtet das Dokument auf neue Platzhalter. */
export function installIconObserver(): void {
  renderIcons()
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') {
        // Alpine setzt `:data-lucide` reaktiv, z. B. im Bestätigungsdialog
        if (record.target.nodeType === Node.ELEMENT_NODE) schedule(record.target as Element)
        continue
      }
      for (const node of record.addedNodes) {
        if (hasPlaceholder(node)) schedule(node)
      }
    }
  })
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: [NAME_ATTR],
  })
}
