/**
 * TipTap-Erweiterung der Satz-Kette (Issue #1584): hält die Lücken als Dekorationen im sichtbaren Text.
 *
 * Drei Arten von Lücken (umbruch.ts):
 * - `block`: ein leerer Block vor dem Knoten (bzw. vor seiner Liste oder seinem Zitat), so hoch wie der Weg bis zum
 *   Satzspiegel der nächsten Seite
 * - `zeile`: ein schwebendes Element in voller Breite am ersten Zeichen einer Zeile. Es schiebt diese Zeile und alle
 *   folgenden nach unten, ohne den Zeilenfall davor zu ändern (auch Blocksatz bleibt unberührt)
 * - `tabelle`: eine leere Tabellenzeile vor der Zeile, die auf die nächste Seite wandert
 *
 * Die Lücken werden nur von SatzSteuerung (steuerung.ts) gesetzt. Bei Änderungen am Text wandern sie mit, bis der
 * nächste Durchlauf sie neu berechnet; so springt beim Tippen nichts.
 */

import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { Luecke } from './vertrag'

export interface SatzZustand {
  luecken: DecorationSet
}

export const SATZ_KEY = new PluginKey<SatzZustand>('satz')

function lueckenElement(luecke: Luecke): HTMLElement {
  const px = `${luecke.px.toFixed(3)}px`
  if (luecke.art === 'tabelle') {
    const zeile = document.createElement('tr')
    zeile.className = 'satz-luecke-tabelle'
    zeile.setAttribute('aria-hidden', 'true')
    const zelle = document.createElement('td')
    // Genau die Spalten der Tabelle: Mehr Spalten veränderten bei festem Tabellenlayout die Breiten
    zelle.colSpan = luecke.spalten ?? 1
    zelle.style.height = px
    zeile.appendChild(zelle)
    return zeile
  }
  const el = document.createElement(luecke.art === 'zeile' ? 'span' : 'div')
  el.className = luecke.art === 'zeile' ? 'satz-luecke-zeile' : 'satz-luecke'
  el.setAttribute('aria-hidden', 'true')
  el.contentEditable = 'false'
  el.style.height = px
  return el
}

export function dekorationen(doc: Parameters<typeof DecorationSet.create>[0], luecken: Luecke[]): DecorationSet {
  return DecorationSet.create(
    doc,
    luecken.map((luecke) =>
      Decoration.widget(luecke.pos, () => lueckenElement(luecke), {
        side: -1,
        ignoreSelection: true,
        // Gleicher Schlüssel = gleiches Element: ProseMirror tauscht unveränderte Lücken nicht aus
        key: `satz-${luecke.art}-${luecke.pos}-${luecke.px.toFixed(2)}`,
      }),
    ),
  )
}

export const Satz = Extension.create({
  name: 'satz',

  addProseMirrorPlugins() {
    return [
      new Plugin<SatzZustand>({
        key: SATZ_KEY,
        state: {
          init: () => ({ luecken: DecorationSet.empty }),
          apply(tr, alt) {
            const neu = tr.getMeta(SATZ_KEY) as SatzZustand | undefined
            if (neu) return neu
            return tr.docChanged ? { luecken: alt.luecken.map(tr.mapping, tr.doc) } : alt
          },
        },
        props: {
          decorations: (state) => SATZ_KEY.getState(state)?.luecken,
        },
      }),
    ]
  },
})
