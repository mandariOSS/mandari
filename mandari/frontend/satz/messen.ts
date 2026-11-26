/**
 * Messen der Satz-Kette (Issue #1584): liest aus dem DOM des Editors, wo Blöcke und Zeilen stehen.
 *
 * Gemessen wird ohne die Lücken der Kette (sie sind beim Messen entfernt): Das ist die „natürliche Lage“, aus der
 * umbruch.ts die Seiten berechnet. Werte sind CSS-Pixel des Layouts relativ zur Oberkante des Textelements; ein Zoom
 * der Ansicht (transform: scale) wird herausgerechnet.
 *
 * Blöcke sind die Einheiten, die als Ganzes oder zeilenweise auf die nächste Seite wandern: Textblöcke (Absätze,
 * Überschriften, auch in Listen und Zitaten), Tabellenzeilen, Bilder, Trennlinien und manuelle Seitenumbrüche.
 */

import type { Node as PMNode, ResolvedPos } from '@tiptap/pm/model'
import type { EditorView } from '@tiptap/pm/view'
import type { BlockArt, MessBlock, Zeile } from './vertrag'

/** Hüllen, vor denen eine Lücke steht, wenn ihr erster Block wandert (Listenpunkt und Zitatlinie wandern mit) */
const HUELLEN = new Set(['listItem', 'taskItem', 'bulletList', 'orderedList', 'taskList', 'blockquote'])
/** Elemente, deren Inhalt nicht zeilenweise geteilt wird */
const ATOME = new Set(['image', 'horizontalRule'])

function ankerVon($pos: ResolvedPos, pos: number): number {
  let anker = pos
  let tiefe = $pos.depth
  while (tiefe > 0 && $pos.index(tiefe) === 0 && HUELLEN.has($pos.node(tiefe).type.name)) {
    anker = $pos.before(tiefe)
    tiefe -= 1
  }
  return anker
}

interface Rahmen {
  /** Oberkante des Textelements auf dem Bildschirm */
  basis: number
  skala: number
}

function lage(r: DOMRect | { top: number; bottom: number }, rahmen: Rahmen): { oben: number; unten: number } {
  return { oben: (r.top - rahmen.basis) / rahmen.skala, unten: (r.bottom - rahmen.basis) / rahmen.skala }
}

/** Zeilenkästen eines Textblocks aus den Rechtecken seines Inhalts */
function zeilenVon(el: HTMLElement, view: EditorView, rahmen: Rahmen): Zeile[] {
  const box = el.getBoundingClientRect()
  const stil = getComputedStyle(el)
  const innenOben =
    box.top + (Number.parseFloat(stil.paddingTop) + Number.parseFloat(stil.borderTopWidth)) * rahmen.skala
  const innenUnten =
    box.bottom - (Number.parseFloat(stil.paddingBottom) + Number.parseFloat(stil.borderBottomWidth)) * rahmen.skala

  const bereich = document.createRange()
  bereich.selectNodeContents(el)
  const rechtecke = Array.from(bereich.getClientRects())
    .filter((r) => r.height > 0 && r.bottom > innenOben - 1 && r.top < innenUnten + 1)
    .sort((a, b) => a.top - b.top || a.left - b.left)

  // Rechtecke zu Zeilen zusammenfassen: Überlappen sie senkrecht zur Hälfte, gehören sie zur selben Zeile
  const gruppen: { top: number; bottom: number; left: number; mitte: number }[] = []
  for (const r of rechtecke) {
    const letzte = gruppen[gruppen.length - 1]
    if (letzte && r.top < letzte.bottom - Math.min(r.height, letzte.bottom - letzte.top) / 2) {
      letzte.bottom = Math.max(letzte.bottom, r.bottom)
      if (r.left < letzte.left) letzte.left = r.left
    } else {
      gruppen.push({ top: r.top, bottom: r.bottom, left: r.left, mitte: 0 })
    }
  }
  if (!gruppen.length) {
    const { oben, unten } = lage({ top: innenOben, bottom: innenUnten }, rahmen)
    return [{ oben, unten, pos: null }]
  }

  // Zeilenkästen: Grenze zwischen zwei Zeilen in der Mitte zwischen ihren Schriftkästen (gleicher Durchschuss oben
  // und unten); die erste beginnt am Innenrand oben, die letzte endet am Innenrand unten
  const zeilen: Zeile[] = []
  for (let i = 0; i < gruppen.length; i++) {
    const g = gruppen[i]
    const top = i === 0 ? innenOben : (gruppen[i - 1].bottom + g.top) / 2
    const bottom = i === gruppen.length - 1 ? innenUnten : (g.bottom + gruppen[i + 1].top) / 2
    let pos: number | null = null
    if (i > 0) {
      const treffer = view.posAtCoords({ left: g.left + 1, top: (g.top + g.bottom) / 2 })
      if (treffer) pos = treffer.pos
    }
    zeilen.push({ ...lage({ top, bottom }, rahmen), pos })
  }
  return zeilen
}

/** Spalten einer Tabellenzeile (verbundene Zellen zählen mit ihrer Spannweite) */
function spaltenVon(zeile: PMNode): number {
  let spalten = 0
  zeile.forEach((zelle) => {
    spalten += Number(zelle.attrs.colspan) || 1
  })
  return Math.max(1, spalten)
}

function artVon(node: PMNode): BlockArt | null {
  const name = node.type.name
  if (name === 'tableRow') return 'zeile'
  if (name === 'pageBreak') return 'umbruch'
  if (name === 'heading') return 'kopf'
  if (node.isTextblock) return 'text'
  if (ATOME.has(name) || node.isAtom) return 'atom'
  return null
}

/**
 * Alle Blöcke des Dokuments in Lesereihenfolge messen. `wurzel` ist das Textelement (`.tiptap`), `skala` der Zoom
 * der Ansicht (1 = 100 %).
 */
export function messen(view: EditorView, wurzel: HTMLElement, skala: number): MessBlock[] {
  const rahmen: Rahmen = { basis: wurzel.getBoundingClientRect().top, skala: skala || 1 }
  const bloecke: MessBlock[] = []
  view.state.doc.descendants((node, pos) => {
    const art = artVon(node)
    if (art === null) return true
    const el = view.nodeDOM(pos)
    if (!(el instanceof HTMLElement)) return false
    const box = el.getBoundingClientRect()
    const { oben, unten } = lage(box, rahmen)
    const $pos = view.state.doc.resolve(pos)
    bloecke.push({
      art,
      oben,
      unten: art === 'umbruch' ? oben : unten,
      zeilen: art === 'text' || art === 'kopf' ? zeilenVon(el, view, rahmen) : [],
      anker: ankerVon($pos, pos),
      mitNaechstem: art === 'kopf',
      zusammen: art !== 'text',
      ...(art === 'zeile' ? { spalten: spaltenVon(node) } : {}),
    })
    return false
  })
  return bloecke
}
