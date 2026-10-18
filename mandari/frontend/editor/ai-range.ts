/**
 * KI-Vorschlag an der markierten Stelle übernehmen (Teil von #856, Konzept I1).
 *
 * Bisher ersetzte „Übernehmen“ das ganze Dokument per `setContent` mit dem Text, den die KI aus dem Stand
 * der Anfrage gemacht hatte: Änderungen anderer seit der Anfrage, Formatierungen und Kommentarmarken
 * gingen verloren. Jetzt gilt:
 *
 * - Beim Start einer KI-Aktion merkt sich der Editor die markierte Stelle (Anfang, Ende, Text). Die
 *   Positionen wandern mit jeder Änderung mit, auch mit denen anderer im gemeinsamen Dokument (Yjs-Relativ-
 *   positionen über `editor.utils`, siehe @tiptap/extension-collaboration).
 * - Beim Übernehmen ersetzt er nur diese Stelle, und nur, wenn ihr Text noch genau der von damals ist.
 *   Hat jemand die Stelle inzwischen geändert, ersetzt er nichts („changed“).
 * - Kommentarmarken in der Stelle bleiben erhalten, soweit sich ihr Text im Vorschlag wiederfindet; findet
 *   sich keiner wieder, trägt der ganze neue Text die erste Marke. Die Kommentare selbst bleiben in jedem
 *   Fall in der Kommentarliste (sie liegen in der Datenbank, nicht im Text).
 * - Vor dem Absenden prüft er, dass der Text vor und hinter der Stelle unverändert bleibt; sonst bricht
 *   er ab („failed“). Nichts außerhalb der Markierung geht verloren.
 */

import type { Editor, MappablePosition } from '@tiptap/core'
import { type Mark, DOMParser as PMDOMParser, type Node as PMNode, Slice } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'

/** Trenner zwischen Blöcken, wenn der Text einer Stelle gelesen und verglichen wird */
const BLOCK_SEPARATOR = '\n'
const COMMENT_MARK = 'commentMark'
/** Blockelemente, an denen der Vorschlag als HTML (statt als reiner Text) erkannt wird */
const BLOCK_TAG = /<(p|ul|ol|li|h[1-6]|blockquote|pre|div)\b/i

export interface AiTarget {
  /** Text der Stelle beim Start der KI-Aktion (Blöcke durch Zeilenumbruch getrennt) */
  readonly text: string
  /** Editor, in dem markiert wurde; wird er neu aufgebaut, ist die Stelle ungültig */
  readonly editor: Editor
  from: MappablePosition
  to: MappablePosition
  /** Verfolgung beenden (nach dem Übernehmen oder Verwerfen) */
  release(): void
}

/** Ergebnis von `applyAiSuggestion` */
export type ApplyResult = 'applied' | 'changed' | 'failed'

interface CommentSpan {
  mark: Mark
  text: string
}

interface TransactionEvent {
  transaction: Transaction
  appendedTransactions?: Transaction[]
}

/** Text einer Stelle so, wie ihn KI-Anfrage und Vergleich sehen */
export function textOfRange(doc: PMNode, from: number, to: number): string {
  return doc.textBetween(from, to, BLOCK_SEPARATOR)
}

/**
 * Markierte Stelle merken und ihre Positionen ab jetzt mitführen; `null` ohne (nichtleere) Markierung.
 */
export function captureAiTarget(editor: Editor): AiTarget | null {
  const { from, to, empty } = editor.state.selection
  if (empty || from >= to) return null
  const text = textOfRange(editor.state.doc, from, to)
  if (!text.trim()) return null

  const target: AiTarget = {
    text,
    editor,
    from: editor.utils.createMappablePosition(from),
    to: editor.utils.createMappablePosition(to),
    release: () => undefined,
  }
  const follow = ({ transaction, appendedTransactions }: TransactionEvent): void => {
    for (const tr of [transaction, ...(appendedTransactions ?? [])]) {
      if (!tr.docChanged) continue
      target.from = editor.utils.getUpdatedPosition(target.from, tr).position
      target.to = editor.utils.getUpdatedPosition(target.to, tr).position
    }
  }
  editor.on('transaction', follow)
  target.release = () => {
    editor.off('transaction', follow)
  }
  return target
}

/** Reiner Text der KI (bereits serverseitig HTML-maskiert) als Absätze; Einzelumbrüche werden zu <br> */
function plainToHtml(content: string): string {
  return content
    .trim()
    .split(/\n{2,}/)
    .map((absatz) => `<p>${absatz.replace(/\n/g, '<br>')}</p>`)
    .join('')
}

/**
 * Vorschlag als Slice für die Stelle: ein einzelner Absatz wird als Inline-Inhalt eingesetzt (der umgebende
 * Absatz bleibt), mehrere Blöcke sind an den Rändern offen und verbinden sich mit dem Text davor und danach.
 */
export function suggestionSlice(editor: Editor, content: string): Slice {
  const html = BLOCK_TAG.test(content) ? content : plainToHtml(content)
  // Inertes Dokument: Der Inhalt wird weder ausgeführt noch geladen (der Server maskiert ohnehin alles
  // außer einfachen Formatierungen ohne Attribute, AIOutputFilter).
  const body = new window.DOMParser().parseFromString(html, 'text/html').body
  const parsed = PMDOMParser.fromSchema(editor.schema).parse(body)
  const blocks = parsed.content
  if (blocks.childCount === 1 && blocks.firstChild?.type.name === 'paragraph') {
    return new Slice(blocks.firstChild.content, 0, 0)
  }
  const openStart = blocks.firstChild?.isTextblock ? 1 : 0
  const openEnd = blocks.lastChild?.isTextblock ? 1 : 0
  return new Slice(blocks, openStart, openEnd)
}

/** Kommentarmarken in der Stelle mit ihrem Text (je Kommentar zusammengefasst, in Dokumentreihenfolge) */
function commentSpans(doc: PMNode, from: number, to: number): CommentSpan[] {
  const spans = new Map<string, CommentSpan>()
  doc.nodesBetween(from, to, (node, pos) => {
    if (!node.isText || !node.text) return
    for (const mark of node.marks) {
      if (mark.type.name !== COMMENT_MARK || !mark.attrs.commentId) continue
      const start = Math.max(from, pos) - pos
      const end = Math.min(to, pos + node.nodeSize) - pos
      const span = spans.get(mark.attrs.commentId) ?? { mark, text: '' }
      span.text += node.text.slice(start, end)
      spans.set(mark.attrs.commentId, span)
    }
  })
  return [...spans.values()]
}

/** Erste Fundstelle von `needle` im Text zwischen `from` und `to` als Dokumentpositionen */
function findText(doc: PMNode, from: number, to: number, needle: string): { from: number; to: number } | null {
  if (!needle) return null
  let text = ''
  const positions: number[] = []
  doc.nodesBetween(from, to, (node, pos) => {
    if (!node.isText || !node.text) return
    for (let i = 0; i < node.text.length; i++) {
      const at = pos + i
      if (at < from || at >= to) continue
      text += node.text[i]
      positions.push(at)
    }
  })
  const index = text.indexOf(needle)
  if (index < 0) return null
  const start = positions[index]
  const last = positions[index + needle.length - 1]
  // Nur zusammenhängende Fundstellen (nicht über einen Blockwechsel hinweg)
  if (start === undefined || last === undefined || last - start !== needle.length - 1) return null
  return { from: start, to: last + 1 }
}

/**
 * KI-Vorschlag an der gemerkten Stelle einsetzen.
 *
 * - `changed`: Die Stelle gibt es so nicht mehr (geändert, gelöscht, Editor neu aufgebaut) – nichts ersetzt.
 * - `failed`: Der Vorschlag ließ sich nicht einsetzen, ohne Text außerhalb der Stelle zu verändern – nichts ersetzt.
 */
export function applyAiSuggestion(editor: Editor, target: AiTarget, content: string): ApplyResult {
  if (target.editor !== editor || editor.isDestroyed) return 'changed'
  const { state } = editor
  const from = target.from.position
  const to = target.to.position
  const size = state.doc.content.size
  if (from >= to || from < 0 || to > size) return 'changed'
  if (textOfRange(state.doc, from, to) !== target.text) return 'changed'

  const before = textOfRange(state.doc, 0, from)
  const after = textOfRange(state.doc, to, size)
  const comments = commentSpans(state.doc, from, to)

  const tr = state.tr.replace(from, to, suggestionSlice(editor, content))
  const start = tr.mapping.map(from, -1)
  const end = tr.mapping.map(to, 1)
  if (textOfRange(tr.doc, 0, start) !== before || textOfRange(tr.doc, end, tr.doc.content.size) !== after) {
    return 'failed'
  }

  // Kommentarmarken: dort, wo ihr Text im Vorschlag steht. Findet sich keiner wieder, trägt der ganze neue
  // Text die erste Marke (Marken eines Typs schließen sich gegenseitig aus, mehr geht nicht ohne Überschreiben).
  const unplaced: Mark[] = []
  for (const { mark, text } of comments) {
    const found = findText(tr.doc, start, end, text)
    if (found) tr.addMark(found.from, found.to, mark)
    else unplaced.push(mark)
  }
  if (unplaced.length && unplaced.length === comments.length && end > start) {
    tr.addMark(start, end, unplaced[0])
  }
  editor.view.dispatch(tr)
  target.release()
  return 'applied'
}
