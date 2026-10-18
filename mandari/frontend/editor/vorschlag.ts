/**
 * Änderungsvorschläge im neuen Antragseditor (Teil von #856, Modus „Vorschlagen“).
 *
 * Ein Vorschlag ist ein Inline-Kommentar an einer markierten Stelle mit dem vorgeschlagenen Ersatz (Server:
 * `apps/work/motions/vorschlaege.py`). Im Text bleibt bis zur Entscheidung alles, wie es ist; die Stelle trägt
 * nur die Kommentarmarke. Annehmen ersetzt die Stelle – und nur, wenn ihr Text noch genau der markierte ist
 * (sonst „changed“, nichts wird überschrieben). Text vor und hinter der Stelle bleibt unberührt.
 */

import type { Editor } from '@tiptap/core'
import type { Node as PMNode } from '@tiptap/pm/model'

const COMMENT_MARK = 'commentMark'
/** Trenner wie beim Anlegen des Kommentars (`_checkTextSelection`: `textBetween(from, to, ' ')`) */
const TRENNER = ' '

export interface Bereich {
  from: number
  to: number
}

/** Von der ersten bis zur letzten Textstelle mit der Kommentarmarke `commentId`; `null`, wenn es keine gibt */
export function markenBereich(doc: PMNode, commentId: string): Bereich | null {
  let from = -1
  let to = -1
  doc.descendants((node, pos) => {
    if (!node.isText) return
    const traegt = node.marks.some((m) => m.type.name === COMMENT_MARK && m.attrs.commentId === commentId)
    if (!traegt) return
    if (from < 0) from = pos
    to = pos + node.nodeSize
  })
  return from < 0 ? null : { from, to }
}

/** Liegt die Auswahl in einem einzigen Absatz (Vorschläge gelten für eine zusammenhängende Stelle)? */
export function inEinemAbsatz(doc: PMNode, from: number, to: number): boolean {
  const $from = doc.resolve(from)
  const $to = doc.resolve(to)
  return $from.sameParent($to) && $from.parent.isTextblock
}

/** Text der Stelle so, wie er beim Anlegen des Vorschlags festgehalten wurde */
export function textDerStelle(doc: PMNode, bereich: Bereich): string {
  return doc.textBetween(bereich.from, bereich.to, TRENNER)
}

/** Antwort des Servers auf „annehmen“ bzw. „ablehnen“ (`MotionCommentResolveView`) */
export interface EntscheidungAntwort {
  vorschlag_angenommen?: boolean | null
  /** Der Vorschlag war vor dieser Anfrage schon entschieden; gespeichert bleibt die frühere Entscheidung */
  bereits_entschieden?: boolean
  /** Die frühere Entscheidung stammt von derselben Person (wiederholte Anfrage, deren Antwort verloren ging) */
  selbst?: boolean
}

/**
 * Darf der Editor die Entscheidung am Text ausführen (Stelle ersetzen bzw. Marke lösen)? Nur, wenn der Server genau
 * diese Entscheidung gespeichert hat und sie nicht schon vorher von jemand anderem getroffen wurde. Sonst arbeitet
 * die Seite mit einem veralteten Stand: Ein abgelehnter Vorschlag darf dann nicht doch in den Text kommen.
 */
export function entscheidungAusfuehren(antwort: EntscheidungAntwort, annehmen: boolean): boolean {
  if (antwort.vorschlag_angenommen !== annehmen) return false
  return antwort.bereits_entschieden !== true || antwort.selbst === true
}

/** Trägt die Stelle mit der Marke `commentId` noch genau den Text `alt`? */
export function vorschlagPasst(editor: Editor, commentId: string, alt: string): boolean {
  const bereich = markenBereich(editor.state.doc, commentId)
  return !!bereich && textDerStelle(editor.state.doc, bereich) === alt
}

/**
 * Vorschlag übernehmen:die Stelle mit der Marke `commentId` durch `neu` ersetzen (leer = streichen) und die
 * Marke entfernen. `changed`, wenn die Stelle fehlt oder ihr Text nicht mehr `alt` ist; dann bleibt alles, wie es ist.
 */
export function vorschlagUebernehmen(
  editor: Editor,
  commentId: string,
  alt: string,
  neu: string,
): 'applied' | 'changed' {
  if (editor.isDestroyed) return 'changed'
  const { state } = editor
  const bereich = markenBereich(state.doc, commentId)
  if (!bereich || textDerStelle(state.doc, bereich) !== alt) return 'changed'
  const vorher = state.doc.textBetween(0, bereich.from, '\n')
  const nachher = state.doc.textBetween(bereich.to, state.doc.content.size, '\n')

  const tr = state.tr
  // insertText übernimmt die Formatierung der Stelle (fett, kursiv …), ohne Zeilenumbrüche
  if (neu) tr.insertText(neu.replace(/\s*\n\s*/g, ' '), bereich.from, bereich.to)
  else tr.delete(bereich.from, bereich.to)
  const ende = tr.mapping.map(bereich.to, 1)
  const start = tr.mapping.map(bereich.from, -1)
  // Sicherheitsnetz: Außerhalb der Stelle darf sich nichts geändert haben
  if (
    tr.doc.textBetween(0, start, '\n') !== vorher ||
    tr.doc.textBetween(ende, tr.doc.content.size, '\n') !== nachher
  ) {
    return 'changed'
  }
  tr.doc.descendants((node, pos) => {
    if (!node.isText) return
    for (const mark of node.marks) {
      if (mark.type.name === COMMENT_MARK && mark.attrs.commentId === commentId) {
        tr.removeMark(pos, pos + node.nodeSize, mark)
      }
    }
  })
  editor.view.dispatch(tr)
  return 'applied'
}
