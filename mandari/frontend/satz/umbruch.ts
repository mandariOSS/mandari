/**
 * Seitenumbruch der Satz-Kette (Issue #1584): reine Funktion ohne DOM, damit sie im Editor und im Druck gleich
 * rechnet und sich ohne Browser prüfen lässt (frontend/satz/test/umbruch.test.mjs).
 *
 * Eingabe sind die Blöcke in natürlicher Lage (ohne Seitenumbrüche gemessen). Die Satzbreite ist auf allen Seiten
 * gleich; deshalb ändern Lücken nur die senkrechte Lage, nie den Zeilenfall. Jede Lücke verschiebt alles Folgende um
 * ihre Höhe (`versatz`).
 *
 * Regeln (schlank, nach dem Vorbild gängiger Textverarbeitungen):
 * - Ein Block, der nicht mehr auf die Seite passt, beginnt oben auf der nächsten Seite.
 * - Absätze werden zwischen Zeilen geteilt; auf jeder Seite stehen mindestens zwei ihrer Zeilen (keine einzelne
 *   Zeile am Seitenende oder -anfang), sonst wandert der Absatz ganz.
 * - Überschriften bleiben mit dem Anfang des nächsten Blocks auf einer Seite.
 * - Tabellenzeilen, Bilder und Überschriften werden nicht geteilt.
 * - Ein manueller Seitenumbruch schiebt den nächsten Block auf die nächste Seite.
 * - Was höher als eine ganze Seite ist, beginnt oben auf einer Seite und ragt darüber hinaus (gezählt in `zuHoch`).
 */

import type { Luecke, MessBlock, Seitengeometrie, Umbruch } from './vertrag'
import { satzOben, satzUnten } from './vertrag'

/** Rundungsreserve: Kanten, die um weniger als das über den Satzspiegel ragen, gelten als passend */
const TOLERANZ = 0.5
/** Mindestzahl an Zeilen eines geteilten Absatzes je Seite */
const MIN_ZEILEN = 2

export function umbrechen(bloecke: MessBlock[], g: Seitengeometrie): Umbruch {
  const luecken: Luecke[] = []
  let versatz = 0
  let seite = 0
  let zuHoch = 0
  /** Manuelle Seitenumbrüche vor dem nächsten Block (zwei hintereinander ergeben eine leere Seite) */
  let umbrueche = 0

  const lueckeVor = (block: MessBlock, px: number): void => {
    if (px <= 0) return
    luecken.push(
      block.art === 'zeile'
        ? { art: 'tabelle', pos: block.anker, px, spalten: block.spalten ?? 1 }
        : { art: 'block', pos: block.anker, px },
    )
    versatz += px
  }
  /** Den Block ab seiner Oberkante an den Anfang der nächsten Seite schieben */
  const ganzWeiter = (block: MessBlock): void => {
    seite += 1
    lueckeVor(block, satzOben(g, seite) - (block.oben + versatz))
  }

  for (let i = 0; i < bloecke.length; i++) {
    const block = bloecke[i]
    if (block.art === 'umbruch') {
      umbrueche += 1
      continue
    }
    // Beginnt der Block erst unterhalb des Satzspiegels (großer Abstand davor), auf die passende Seite weiterzählen
    // und an den Anfang ihres Satzspiegels rücken, nicht in den Rand oder die Lücke zwischen den Seiten
    while (block.oben + versatz >= satzUnten(g, seite) - TOLERANZ) seite += 1
    if (block.oben + versatz < satzOben(g, seite) - TOLERANZ) {
      lueckeVor(block, satzOben(g, seite) - (block.oben + versatz))
    }
    // Jeder Umbruch beginnt eine neue Seite; steht der Block schon oben auf einer Seite, die der natürliche Fluss
    // gerade begonnen hat, zählt der erste Umbruch als erledigt
    if (umbrueche > 0) {
      const schonOben = block.oben + versatz <= satzOben(g, seite) + TOLERANZ && seite > 0
      for (let n = schonOben ? 1 : 0; n < umbrueche; n++) ganzWeiter(block)
      umbrueche = 0
    }

    const amSeitenanfang = block.oben + versatz <= satzOben(g, seite) + TOLERANZ
    const grenze = satzUnten(g, seite) + TOLERANZ

    if (block.unten + versatz <= grenze) {
      // Passt. Überschrift: Bleibt vom nächsten Block nichts auf dieser Seite, mit ihm weiterwandern.
      const naechster = bloecke[i + 1]
      if (block.mitNaechstem && naechster && naechster.art !== 'umbruch' && !amSeitenanfang) {
        const zeilen = naechster.zeilen
        // Teilbar ist ein Absatz erst ab 2 + 2 Zeilen: darunter muss er ganz auf die Seite passen
        const anfang =
          !naechster.zusammen && zeilen.length >= 2 * MIN_ZEILEN ? zeilen[MIN_ZEILEN - 1].unten : naechster.unten
        if (anfang + versatz > grenze) ganzWeiter(block)
      }
      continue
    }

    const teilbar = !block.zusammen && block.zeilen.length >= 2 * MIN_ZEILEN
    if (!teilbar) {
      if (!amSeitenanfang) ganzWeiter(block)
      if (block.unten + versatz > satzUnten(g, seite) + TOLERANZ) zuHoch += 1
      continue
    }

    // Absatz zwischen Zeilen teilen, so oft wie nötig
    const zeilen = block.zeilen
    let erste = 0
    let verschoben = amSeitenanfang
    for (;;) {
      const grenzeHier = satzUnten(g, seite) + TOLERANZ
      let letzte = erste - 1
      while (letzte + 1 < zeilen.length && zeilen[letzte + 1].unten + versatz <= grenzeHier) letzte += 1
      if (letzte === zeilen.length - 1) break
      // Mindestens zwei Zeilen auf die nächste Seite mitnehmen
      if (zeilen.length - (letzte + 1) < MIN_ZEILEN) letzte = zeilen.length - 1 - MIN_ZEILEN
      if (letzte - erste + 1 < MIN_ZEILEN) {
        if (erste === 0 && !verschoben) {
          // Zu wenig Platz für den Absatzanfang: ganz auf die nächste Seite, dort neu verteilen
          ganzWeiter(block)
          verschoben = true
          continue
        }
        // Eine Seite ohne Platz für zwei Zeilen (sehr große Zeilen): wenigstens eine Zeile setzen
        letzte = Math.max(erste, letzte)
        if (letzte >= zeilen.length - 1) break
      }
      const naechste = zeilen[letzte + 1]
      if (naechste.pos === null) {
        // Ohne Position lässt sich hier nicht trennen: Der Rest ragt über die Seite
        zuHoch += 1
        break
      }
      seite += 1
      const px = satzOben(g, seite) - (naechste.oben + versatz)
      if (px > 0) {
        luecken.push({ art: 'zeile', pos: naechste.pos, px })
        versatz += px
      }
      erste = letzte + 1
      verschoben = true
    }
  }

  // Die letzte Seite ist die, auf der der letzte Inhalt endet (ein Umbruch am Ende beginnt eine leere Seite)
  const inhalt = bloecke.filter((b) => b.art !== 'umbruch')
  const letzter = inhalt[inhalt.length - 1]
  if (letzter) {
    while (letzter.unten + versatz > satzUnten(g, seite) + TOLERANZ && seite < 10_000) seite += 1
  }
  seite += umbrueche
  return { seiten: seite + 1, luecken, zuHoch }
}
