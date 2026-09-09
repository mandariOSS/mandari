/**
 * Lucide-Icons: einmalige Initialisierung plus automatische Nachinitialisierung.
 *
 * Templates schreiben `<i data-lucide="name">`. Ein MutationObserver ersetzt neue
 * Platzhalter, egal ob sie per HTMX-Swap, Alpine (`x-for`, `x-if`) oder direkt per DOM
 * eingefügt wurden. Manuelle `lucide.createIcons()`-Aufrufe sind damit überflüssig.
 */

import { createIcons, icons } from 'lucide'

const NAME_ATTR = 'data-lucide'

function hasPlaceholder(node: Node): node is Element {
  if (node.nodeType !== Node.ELEMENT_NODE) return false
  const el = node as Element
  return el.hasAttribute(NAME_ATTR) || el.querySelector(`[${NAME_ATTR}]`) !== null
}

/** Ersetzt alle `[data-lucide]`-Platzhalter unterhalb von `root` (Standard: document). */
export function renderIcons(root: ParentNode = document): void {
  createIcons({ icons, nameAttr: NAME_ATTR, root: root as Element | Document })
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
