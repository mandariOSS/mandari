/**
 * Absatzformat (Issue #1600): Abstand vor und nach dem Absatz, Zeilenabstand, Einzüge, Tabulatorweite und eine eigene
 * Schrift des Absatzes. Treu übernommene PDFs (apps/work/motions/pdf_treu.py) bringen diese Angaben als Stil am Absatz
 * mit; ohne diese Erweiterung verwürfe TipTap sie beim Laden und der Text verlöre seine Lage auf der Seite.
 *
 * Die Werte bleiben, wie sie im HTML stehen (z. B. `12.5pt`). Die Positivliste des Servers (apps/work/sanitize.py)
 * lässt nur Längen zu; Schrift und Größe prüft sie gegen den Schriftsatz.
 */

import { Extension } from '@tiptap/core'
import Paragraph from '@tiptap/extension-paragraph'

type Stile = Record<string, string>

/** Attributname → CSS-Eigenschaft (Absätze und Überschriften) */
const ABSATZ: Stile = {
  abstandOben: 'margin-top',
  abstandUnten: 'margin-bottom',
  zeilenhoehe: 'line-height',
  einzugLinks: 'padding-left',
  einzugErste: 'text-indent',
  tabweite: 'tab-size',
  absatzSchrift: 'font-family',
  absatzGroesse: 'font-size',
  wortabstand: 'word-spacing',
}

/** Textläufe: Abstand davor (große Lücke in der Zeile des Originals, etwa Unterschriften nebeneinander) */
const LAUF: Stile = {
  laufAbstand: 'margin-left',
  laufWortabstand: 'word-spacing',
  laufZeichenabstand: 'letter-spacing',
}

/** Bilder im Textfluss: Abstand davor, Lage von links, Breite */
const BILD: Stile = {
  bildOben: 'margin-top',
  bildUnten: 'margin-bottom',
  bildLinks: 'margin-left',
  bildBreite: 'width',
}

function attribute(stile: Stile) {
  return Object.fromEntries(
    Object.entries(stile).map(([name, eigenschaft]) => [
      name,
      {
        default: null,
        parseHTML: (el: HTMLElement) => el.style.getPropertyValue(eigenschaft).trim() || null,
        renderHTML: (werte: Record<string, unknown>) => {
          const wert = werte[name]
          return typeof wert === 'string' && wert ? { style: `${eigenschaft}: ${wert}` } : {}
        },
      },
    ]),
  )
}

export const Absatzformat = Extension.create({
  name: 'absatzformat',

  addGlobalAttributes() {
    return [
      { types: ['paragraph', 'heading'], attributes: attribute(ABSATZ) },
      { types: ['image'], attributes: attribute(BILD) },
      { types: ['textStyle'], attributes: attribute(LAUF) },
    ]
  },
})

/**
 * Absatz, der Tabulatoren behält: Ein Absatz mit Tabulatorweite (Listenzeichen ⇥ Text, „Ratsantrag ⇥ Datum“) wird
 * ohne Zusammenfassen des Leerraums gelesen. Sonst machte ProseMirror aus dem Tabulator ein Leerzeichen.
 */
export const AbsatzMitTabulator = Paragraph.extend({
  parseHTML() {
    return [{ tag: 'p[style*="tab-size"]', preserveWhitespace: 'full' as const }, ...(this.parent?.() ?? [])]
  },
})
