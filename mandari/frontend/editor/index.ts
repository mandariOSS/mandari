/**
 * Mandari Document Editor — TipTap/ProseMirror based
 *
 * Vite-Einstieg der Editor-Seiten: setzt `window.MandariEditor` (Fabriken, Diff,
 * Briefkopf) und registriert die Alpine-Komponenten `documentEditor`
 * (frontend/alpine/document-editor.ts), `preparationApp`
 * (frontend/alpine/prepare-meeting.ts), `vorbereitung` (frontend/alpine/vorbereitung.ts) und die der
 * Sitzungsansicht (frontend/alpine/faction-session.ts, frontend/alpine/session-notes.ts).
 */

import { Editor } from '@tiptap/core'
import { isChangeOrigin } from '@tiptap/extension-collaboration'
import Link from '@tiptap/extension-link'
import Underline from '@tiptap/extension-underline'
import { Placeholder } from '@tiptap/extensions'
import StarterKit from '@tiptap/starter-kit'
import Alpine from 'alpinejs'
import { documentEditor } from '../alpine/document-editor'
import {
  anwesenheitsListe,
  beschlussLeiste,
  fraktionssitzung,
  sitzungsMenue,
  sitzungsUnterlagen,
  topKopf,
  topNeu,
  unterlageAnhaengen,
  zustaendigAuswahl,
} from '../alpine/faction-session'
import { preparationApp } from '../alpine/prepare-meeting'
import { type NotizenFormat, sitzungsNotizen } from '../alpine/session-notes'
import { vorbereitung } from '../alpine/vorbereitung'
import { SatzSteuerung } from '../satz/steuerung'
import { dokumentErweiterungen, dokumentStarterKit } from './basis'
import type { CollabOptions, CollabResult, CollabStatus, CollabUser, ReloadReason } from './collaboration'
import { initCollaboration } from './collaboration'
import { renderDiff } from './diff'
import { CommentMark } from './extensions/comment-mark'
import { type ErwaehnbarePerson, Erwaehnung } from './extensions/erwaehnung'
import { FindReplace } from './extensions/find-replace'
import { PageBreakNode } from './extensions/page-break-node'
import { YjsUndo } from './extensions/yjs-undo'
import { renderLetterhead } from './letterhead'
import { cleanPastedHtml } from './paste-cleanup'
import { ladePdfJs } from './pdfjs-laden'

export interface EditorOptions {
  element: HTMLElement
  content: string
  editable: boolean
  onUpdate?: (html: string) => void
  onSelectionUpdate?: (state: FormatState) => void
  onPageCount?: (count: number, currentPage: number) => void
  /**
   * Echte Seiten über die Satz-Kette (frontend/satz/, Issue #1584) statt der geschätzten Seitenlücken: Die Lücken setzt
   * eine SatzSteuerung, sobald die Seitenansicht an ist.
   */
  satz?: boolean
  /** Suchen & Ersetzen: wird bei Änderung der Trefferliste aufgerufen */
  onSearchUpdate?: (current: number, total: number) => void
  placeholder?: string
}

export interface CollaborativeEditorOptions extends EditorOptions {
  /**
   * Inhalt geändert. ``anfangsstand``: Die Änderung lädt nur den gespeicherten Stand (Zustand vom Server, das erste
   * Zeichnen vor dessen Eintreffen oder das Auffüllen aus dem gespeicherten Text) und ist keine Eingabe (#1018).
   */
  onUpdate?: (html: string, anfangsstand?: boolean) => void
  /** WebSocket URL for collaboration */
  wsUrl: string
  /** Current user info */
  user: { name: string; color: string }
  /** Called when presence list changes */
  onPresenceChange?: (users: CollabUser[]) => void
  /** Called on connection status change */
  onStatusChange?: (status: 'connecting' | 'connected' | 'disconnected') => void
  /** Called when server sends initial state (hasState=true means server had saved Yjs state) */
  onInitialState?: (hasState: boolean) => void
  /**
   * Der Anfangsstand steht im Editor: Zustand vom Server eingespielt bzw. aus dem gespeicherten Text aufgefüllt (oder
   * ein anderes Fenster war schneller). Bis dahin soll der Editor nicht bearbeitbar sein: Eine Eingabe in das noch
   * leere Dokument verhinderte das Auffüllen, und die nächste Sicherung ersetzte den gespeicherten Text (#1018).
   */
  onAnfangsstandGeladen?: () => void
  /** Called when the server requests a document reload (e.g. after revision restore) */
  onReloadRequired?: (reason?: ReloadReason) => void
  /**
   * Server persisted a yjs_save; fingerprint of the stored HTML for conflict detection (#184). ``marke``: was
   * ``sicherungsMarke`` beim Senden dieser Sicherung lieferte (#1018).
   */
  onPersisted?: (contentHash: string, marke?: number) => void
  /** Kennzeichen des Stands beim Senden einer Sicherung (yjs_save), siehe ``CollabOptions.sicherungsMarke`` */
  sicherungsMarke?: () => number
}

/** Kennzeichen der Transaktion, die den gespeicherten Text in ein frisches Kollaborationsdokument füllt (#1018) */
const ANFANGSSTAND_META = 'mandariAnfangsstand'

export interface FormatState {
  bold: boolean
  italic: boolean
  underline: boolean
  strike: boolean
  header: number | false
  list: 'bullet' | 'ordered' | 'task' | false
  blockquote: boolean
  link: boolean
  textAlign: string
  textColor: string | false
  highlight: boolean
  table: boolean
  /** Schriftart der Auswahl (erste Angabe von font-family) oder leer = Schrift des Dokuments */
  fontFamily: string
  /** Schriftgröße der Auswahl (z. B. „12pt“) oder leer = Größe des Dokuments */
  fontSize: string
}

function getFormatState(editor: Editor): FormatState {
  return {
    bold: editor.isActive('bold'),
    italic: editor.isActive('italic'),
    underline: editor.isActive('underline'),
    strike: editor.isActive('strike'),
    header: editor.isActive('heading', { level: 1 })
      ? 1
      : editor.isActive('heading', { level: 2 })
        ? 2
        : editor.isActive('heading', { level: 3 })
          ? 3
          : false,
    list: editor.isActive('taskList')
      ? 'task'
      : editor.isActive('bulletList')
        ? 'bullet'
        : editor.isActive('orderedList')
          ? 'ordered'
          : false,
    blockquote: editor.isActive('blockquote'),
    link: editor.isActive('link'),
    textAlign: editor.isActive({ textAlign: 'center' })
      ? 'center'
      : editor.isActive({ textAlign: 'right' })
        ? 'right'
        : editor.isActive({ textAlign: 'justify' })
          ? 'justify'
          : 'left',
    textColor: editor.getAttributes('textStyle').color || false,
    highlight: editor.isActive('highlight'),
    table: editor.isActive('table'),
    fontFamily: ((editor.getAttributes('textStyle').fontFamily as string | undefined) ?? '')
      .split(',')[0]
      .trim()
      .replace(/^["']|["']$/g, ''),
    fontSize: (editor.getAttributes('textStyle').fontSize as string | undefined) ?? '',
  }
}

/** Base extensions shared between solo and collaborative editors (frontend/editor/basis.ts, auch für den Druck) */
function getBaseExtensions(options: EditorOptions) {
  return dokumentErweiterungen(options)
}

/**
 * Gemeinsame editorProps: Paste-Cleanup für Word/Google-Docs-HTML; Name des Textfelds (TipTap setzt
 * `role="textbox"`, ohne Namen bleibt es für Bildschirmleser unbeschriftet)
 */
function getEditorProps() {
  return {
    attributes: { 'aria-label': 'Text des Dokuments', 'aria-multiline': 'true' },
    transformPastedHTML: (html: string) => cleanPastedHtml(html),
  }
}

/** Create a standalone (non-collaborative) TipTap editor */
export function createEditor(options: EditorOptions): Editor {
  const editor = new Editor({
    element: options.element,
    editable: options.editable,
    content: options.content,
    editorProps: getEditorProps(),
    extensions: [dokumentStarterKit(), ...getBaseExtensions(options)],
    onUpdate({ editor }) {
      if (options.onUpdate) {
        options.onUpdate(editor.getHTML())
      }
    },
    onTransaction({ editor }) {
      if (options.onSelectionUpdate) {
        options.onSelectionUpdate(getFormatState(editor))
      }
    },
  })

  return editor
}

export interface NotesEditorOptions {
  element: HTMLElement
  content: string
  placeholder?: string
  /** Vorschlagsliste für @-Erwähnungen; ohne Liste kein Erwähnen */
  personen?: ErwaehnbarePerson[]
  /** Eigene Mitgliedschaft (Auslöser einer Erwähnung) */
  ich?: string
  onUpdate?: () => void
  onFormat?: (format: NotizenFormat) => void
}

/**
 * Erweiterungen der Notizen: Absätze, Zwischenüberschriften, Fett, Kursiv, Unterstrichen, Durchgestrichen (aus dem
 * StarterKit), Listen, Links, Erwähnen.
 */
function notizenErweiterungen(options: NotesEditorOptions, live: boolean) {
  return [
    StarterKit.configure({
      heading: { levels: [2, 3] },
      link: false,
      underline: false,
      trailingNode: false,
      codeBlock: false,
      horizontalRule: false,
      // Gemeinsam: Rückgängig über Yjs (nur eigene Änderungen)
      ...(live ? { undoRedo: false as const } : {}),
    }),
    Underline,
    Link.configure({ openOnClick: false, autolink: true, protocols: ['http', 'https', 'mailto'] }),
    Placeholder.configure({ placeholder: options.placeholder || '' }),
    ...(options.personen?.length ? [Erwaehnung.configure({ personen: options.personen, von: options.ich ?? '' })] : []),
  ]
}

function notizenFormat(editor: Editor): NotizenFormat {
  const state = getFormatState(editor)
  return {
    bold: state.bold,
    italic: state.italic,
    underline: state.underline,
    strike: state.strike,
    heading: state.header !== false,
    bulletList: state.list === 'bullet',
    orderedList: state.list === 'ordered',
    link: state.link,
  }
}

const NOTIZEN_ATTRIBUTE = { 'aria-label': 'Notizen', 'aria-multiline': 'true', role: 'textbox' }

/**
 * Schlanker Editor für die Notizen eines TOPs in der Fraktionssitzung (Issue #874): Absätze,
 * Zwischenüberschriften, Fett, Kursiv, Unterstrichen, Listen und Links – ohne Seitenumbrüche, Tabellen,
 * Bilder und Briefkopf. Gespeichert wird serverseitig nur die Positivliste (apps/work/sanitize.py).
 */
export function createNotesEditor(options: NotesEditorOptions): Editor {
  return new Editor({
    element: options.element,
    editable: true,
    content: options.content,
    editorProps: { ...getEditorProps(), attributes: NOTIZEN_ATTRIBUTE },
    extensions: notizenErweiterungen(options, false),
    onUpdate() {
      options.onUpdate?.()
    },
    onTransaction({ editor }) {
      options.onFormat?.(notizenFormat(editor))
    },
  })
}

export interface LiveNotesEditorOptions extends NotesEditorOptions {
  /** WebSocket des TOPs (apps/work/faction/live.py) */
  wsUrl: string
  user: { name: string; color: string }
  /** Nur eigene Änderungen (nicht die über die Synchronisierung eingetroffenen) */
  onLocalUpdate?: () => void
  /** Eine Änderung anderer ist eingetroffen */
  onRemoteUpdate?: () => void
  onJsonMessage?: (msg: any) => void
  onStatusChange?: (status: CollabStatus) => void
  onPersisted?: (stand: string) => void
  onReloadRequired?: (reason?: ReloadReason) => void
}

/**
 * Notizen-Editor für das Live-Mitschreiben (Issue #874): wie createNotesEditor, dazu Yjs über den WebSocket des
 * TOPs mit den Cursorn der anderen. Gespeichert wird nur über `collab.flush()` (die Alpine-Komponente entscheidet,
 * wann der Editor einen gültigen Stand hat); der Inhalt kommt aus dem Zustand des Servers oder wird von genau einem
 * Editor aus den Notizen gefüllt.
 */
export function createLiveNotesEditor(options: LiveNotesEditorOptions): { editor: Editor; collab: CollabResult } {
  let editor: Editor
  const collab = initCollaboration({
    wsUrl: options.wsUrl,
    user: options.user,
    autoSave: false,
    onStatusChange: options.onStatusChange,
    onJsonMessage: options.onJsonMessage,
    onPersisted: options.onPersisted,
    onReloadRequired: options.onReloadRequired,
    getHtml: () => {
      try {
        return editor && !editor.isDestroyed ? editor.getHTML() : ''
      } catch {
        return ''
      }
    },
  })
  collab.ydoc.on('update', (_update: Uint8Array, origin: unknown) => {
    if (collab.isRemoteOrigin(origin)) options.onRemoteUpdate?.()
  })
  editor = new Editor({
    element: options.element,
    editable: true,
    editorProps: { ...getEditorProps(), attributes: NOTIZEN_ATTRIBUTE },
    extensions: [...notizenErweiterungen(options, true), ...collab.extensions, YjsUndo],
    onUpdate({ transaction }) {
      if (!isChangeOrigin(transaction)) options.onLocalUpdate?.()
    },
    onTransaction({ editor }) {
      options.onFormat?.(notizenFormat(editor))
    },
  })
  return { editor, collab }
}

/**
 * Create a collaborative TipTap editor with real-time sync via Yjs + WebSocket.
 *
 * Returns both the editor and a collab object with destroy() for cleanup.
 */
export function createCollaborativeEditor(options: CollaborativeEditorOptions): {
  editor: Editor
  collab: CollabResult
} {
  // editor is declared here so the onInitialState closure can reference it.
  // By the time the async callback fires, editor will be assigned.
  // biome-ignore lint/style/useConst: wird erst nach initCollaboration() zugewiesen, Closure braucht die Bindung
  let editor: Editor
  // Bis der gespeicherte Stand vom Server da ist, bringt die Verbindung nur diesen Stand (und das erste Zeichnen des
  // leeren Dokuments): keine Eingabe dieses Fensters (#1018). Der Provider meldet onInitialState nach dem Einspielen.
  let anfangsstandGeladen = false
  // Erst wenn der Anfangsstand im Editor steht (nach dem Auffüllen), darf gesichert werden (#1018)
  let anfangsstandFertig = false
  const anfangsstandMelden = () => {
    anfangsstandFertig = true
    options.onAnfangsstandGeladen?.()
  }

  const collab = initCollaboration({
    wsUrl: options.wsUrl,
    user: options.user,
    onPresenceChange: options.onPresenceChange,
    onStatusChange: options.onStatusChange,
    getHtml: () => {
      try {
        return editor && !editor.isDestroyed ? editor.getHTML() : ''
      } catch (e) {
        return ''
      }
    },
    onReloadRequired: options.onReloadRequired,
    onPersisted: options.onPersisted,
    sicherungsMarke: options.sicherungsMarke,
    sicherungBereit: () => anfangsstandFertig,
    onInitialState: (hasState: boolean) => {
      anfangsstandGeladen = true
      if (!hasState && options.content) {
        // Server has no saved Yjs state — seed from HTML content.
        // Kurz warten und nur seeden, wenn nicht inzwischen ein anderer
        // Client seinen Zustand gesynct hat (verhindert doppelten Inhalt
        // bei nahezu gleichzeitigem Öffnen eines frischen Dokuments).
        setTimeout(() => {
          if (!editor || editor.isDestroyed) return
          if (editor.isEmpty) {
            // Der gespeicherte Text ist keine Eingabe: als Anfangsstand kennzeichnen (#1018)
            editor.chain().setMeta(ANFANGSSTAND_META, true).setContent(options.content).run()
          }
          anfangsstandMelden()
        }, 400)
      } else {
        anfangsstandMelden()
      }
      // Forward to caller if they want to know
      options.onInitialState?.(hasState)
    },
  })

  editor = new Editor({
    element: options.element,
    editable: options.editable,
    editorProps: getEditorProps(),
    // Content is loaded via Yjs (from server state or seeded in onInitialState)
    extensions: [
      // Ohne eingebautes Rückgängig: In der Kollaboration übernimmt Yjs (YjsUndo)
      dokumentStarterKit(true),
      ...getBaseExtensions(options),
      ...collab.extensions,
      YjsUndo,
    ],
    onUpdate({ editor, transaction }) {
      if (options.onUpdate) {
        const anfangsstand =
          transaction.getMeta(ANFANGSSTAND_META) === true || (!anfangsstandGeladen && isChangeOrigin(transaction))
        options.onUpdate(editor.getHTML(), anfangsstand)
      }
    },
    onTransaction({ editor }) {
      if (options.onSelectionUpdate) {
        options.onSelectionUpdate(getFormatState(editor))
      }
    },
  })

  return { editor, collab }
}

export type { CollabOptions, CollabResult, CollabUser }
export {
  CommentMark,
  cleanPastedHtml,
  Editor,
  FindReplace,
  getFormatState,
  initCollaboration,
  PageBreakNode,
  renderDiff,
  renderLetterhead,
}

/** Satz-Kette der Seitenansicht (frontend/satz/) mit pdf.js für ein Briefpapier als PDF */
function satzSteuerung(
  editor: Editor,
  rahmen: HTMLElement,
  optionen: ConstructorParameters<typeof SatzSteuerung>[2],
): SatzSteuerung {
  return new SatzSteuerung(editor, rahmen, { ...optionen, pdfjs: ladePdfJs })
}

// Globales Objekt für die Alpine-Komponenten in den Templates (ES-Modul via Vite)
const MandariEditor = {
  createEditor,
  createCollaborativeEditor,
  createNotesEditor,
  createLiveNotesEditor,
  Editor,
  getFormatState,
  CommentMark,
  FindReplace,
  PageBreakNode,
  cleanPastedHtml,
  renderDiff,
  renderLetterhead,
  initCollaboration,
  satzSteuerung,
}
declare global {
  interface Window {
    MandariEditor: typeof MandariEditor
  }
}
window.MandariEditor = MandariEditor

// ---- Alpine-Komponenten der Editor-Seiten ------------------------------------
// Registrierung vor Alpine.start() (main.ts startet erst bei DOMContentLoaded);
// Vite teilt den Alpine-Chunk, es ist dieselbe Instanz wie in main.ts.
Alpine.data('documentEditor', documentEditor)
Alpine.data('preparationApp', preparationApp)
// Sitzungsvorbereitung im neuen Design (#856), baut auf preparationApp auf
Alpine.data('vorbereitung', vorbereitung)
// Sitzungsansicht der laufenden Fraktionssitzung (Issue #874)
Alpine.data('fraktionssitzung', fraktionssitzung)
Alpine.data('sitzungsMenue', sitzungsMenue)
Alpine.data('topKopf', topKopf)
Alpine.data('sitzungsUnterlagen', sitzungsUnterlagen)
Alpine.data('unterlageAnhaengen', unterlageAnhaengen)
Alpine.data('beschlussLeiste', beschlussLeiste)
Alpine.data('anwesenheitsListe', anwesenheitsListe)
Alpine.data('topNeu', topNeu)
Alpine.data('zustaendigAuswahl', zustaendigAuswahl)
Alpine.data('sitzungsNotizen', sitzungsNotizen)
