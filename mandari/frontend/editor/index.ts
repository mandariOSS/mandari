/**
 * Mandari Document Editor — TipTap/ProseMirror based
 *
 * Exports `window.MandariEditor` (IIFE bundle) for use with Alpine.js.
 */

import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import Underline from '@tiptap/extension-underline'
import Placeholder from '@tiptap/extension-placeholder'
import CharacterCount from '@tiptap/extension-character-count'
import Link from '@tiptap/extension-link'
import TextAlign from '@tiptap/extension-text-align'
import Highlight from '@tiptap/extension-highlight'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableHeader from '@tiptap/extension-table-header'
import TableCell from '@tiptap/extension-table-cell'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import Color from '@tiptap/extension-color'
import TextStyle from '@tiptap/extension-text-style'
import Image from '@tiptap/extension-image'
import { CommentMark } from './extensions/comment-mark'
import { Indent } from './extensions/indent'
import { PageBreaks } from './extensions/page-breaks'
import { YjsUndo } from './extensions/yjs-undo'
import { SlashCommands } from './extensions/slash-commands'
import { renderDiff } from './diff'
import { renderLetterhead } from './letterhead'
import { initCollaboration } from './collaboration'
import type { CollabOptions, CollabUser, CollabResult } from './collaboration'

export interface EditorOptions {
  element: HTMLElement
  content: string
  editable: boolean
  onUpdate?: (html: string) => void
  onSelectionUpdate?: (state: FormatState) => void
  onPageCount?: (count: number, currentPage: number) => void
  placeholder?: string
}

export interface CollaborativeEditorOptions extends EditorOptions {
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
}

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
  table: boolean
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
    table: editor.isActive('table'),
  }
}

/** Base extensions shared between solo and collaborative editors */
function getBaseExtensions(options: EditorOptions) {
  return [
    Underline,
    Placeholder.configure({
      placeholder: options.placeholder || 'Beginnen Sie hier mit dem Schreiben...',
    }),
    CharacterCount,
    Link.configure({
      openOnClick: false,
      HTMLAttributes: {
        class: 'text-primary-600 underline hover:text-primary-700',
      },
    }),
    TextAlign.configure({
      types: ['heading', 'paragraph'],
    }),
    Highlight.configure({
      multicolor: true,
    }),
    // Tables
    Table.configure({
      resizable: true,
      HTMLAttributes: {
        class: 'editor-table',
      },
    }),
    TableRow,
    TableHeader,
    TableCell,
    // Task Lists / Checklists
    TaskList,
    TaskItem.configure({
      nested: true,
    }),
    // Text Color
    TextStyle,
    Color,
    // Images
    Image.configure({
      inline: false,
      allowBase64: true,
      HTMLAttributes: {
        class: 'editor-image',
      },
    }),
    CommentMark,
    Indent,
    PageBreaks.configure({
      onPageCount: options.onPageCount,
    }),
    // Slash Commands
    SlashCommands,
  ]
}

/** Create a standalone (non-collaborative) TipTap editor */
export function createEditor(options: EditorOptions): Editor {
  const editor = new Editor({
    element: options.element,
    editable: options.editable,
    content: options.content,
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
      }),
      ...getBaseExtensions(options),
    ],
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

/**
 * Create a collaborative TipTap editor with real-time sync via Yjs + WebSocket.
 *
 * Returns both the editor and a collab object with destroy() for cleanup.
 */
export function createCollaborativeEditor(
  options: CollaborativeEditorOptions
): { editor: Editor; collab: CollabResult } {
  // editor is declared here so the onInitialState closure can reference it.
  // By the time the async callback fires, editor will be assigned.
  let editor: Editor

  const collab = initCollaboration({
    wsUrl: options.wsUrl,
    user: options.user,
    onPresenceChange: options.onPresenceChange,
    onStatusChange: options.onStatusChange,
    onInitialState: (hasState: boolean) => {
      if (!hasState && options.content) {
        // Server has no saved Yjs state — seed from HTML content
        editor.commands.setContent(options.content)
      }
      // Forward to caller if they want to know
      options.onInitialState?.(hasState)
    },
  })

  editor = new Editor({
    element: options.element,
    editable: options.editable,
    // Content is loaded via Yjs (from server state or seeded in onInitialState)
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        // Disable built-in history in collab mode — Yjs handles undo/redo
        history: false,
      }),
      ...getBaseExtensions(options),
      ...collab.extensions,
      YjsUndo,
    ],
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

  return { editor, collab }
}

export {
  Editor,
  getFormatState,
  CommentMark,
  renderDiff,
  renderLetterhead,
  initCollaboration,
}
export type { CollabOptions, CollabUser, CollabResult }
