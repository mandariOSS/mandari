/**
 * Erweiterungen des Dokumenteditors (Anträge, Vorlagen), gemeinsam für den Editor (frontend/editor/index.ts) und den
 * Druck der Satz-Kette (frontend/satz/druck.ts). Beide setzen denselben Text deshalb mit demselben Schema und
 * denselben Knoten; nur so stimmen Seiten im Editor und im PDF überein (Issue #1584).
 *
 * Ohne Alpine und ohne Oberfläche: Der Druck lädt nur dieses Modul und die Satz-Kette.
 */

import Highlight from '@tiptap/extension-highlight'
import Image from '@tiptap/extension-image'
import Link from '@tiptap/extension-link'
import { TaskItem, TaskList } from '@tiptap/extension-list'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import TextAlign from '@tiptap/extension-text-align'
import { Color, FontFamily, FontSize, TextStyle } from '@tiptap/extension-text-style'
import Underline from '@tiptap/extension-underline'
import { CharacterCount, Placeholder } from '@tiptap/extensions'
import StarterKit from '@tiptap/starter-kit'
import { Satz } from '../satz/erweiterung'
import { CommentMark } from './extensions/comment-mark'
import { FindReplace } from './extensions/find-replace'
import { Indent } from './extensions/indent'
import { PageBreakNode } from './extensions/page-break-node'
import { PageBreaks } from './extensions/page-breaks'
import { SlashCommands } from './extensions/slash-commands'

export interface DokumentErweiterungsOptionen {
  placeholder?: string
  /** Echte Seiten über die Satz-Kette statt geschätzter Seitenlücken (bisheriger Editor) */
  satz?: boolean
  onPageCount?: (count: number, currentPage: number) => void
  onSearchUpdate?: (current: number, total: number) => void
  /** Ohne Rückgängig (Kollaboration: Yjs übernimmt) */
  ohneVerlauf?: boolean
}

/** StarterKit des Dokumenteditors */
export function dokumentStarterKit(ohneVerlauf = false) {
  return StarterKit.configure({
    heading: { levels: [1, 2, 3] },
    // Link und Underline konfigurieren wir selbst (siehe dokumentErweiterungen);
    // TrailingNode würde das Dokumentende verändern (Briefkopf/Seitenzählung).
    link: false,
    underline: false,
    trailingNode: false,
    ...(ohneVerlauf ? { undoRedo: false as const } : {}),
  })
}

/** Erweiterungen nach dem StarterKit (Solo, Kollaboration und Druck) */
export function dokumentErweiterungen(options: DokumentErweiterungsOptionen) {
  return [
    Underline,
    Placeholder.configure({
      placeholder: options.placeholder ?? 'Beginnen Sie hier mit dem Schreiben...',
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
    // Textfarbe, Schriftart und -größe (nur Schriften des Satzes, apps/common/schriften.py; die Positivliste
    // apps/work/sanitize.py verwirft alles andere)
    TextStyle,
    Color,
    FontFamily,
    FontSize,
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
    // Seiten: echte Seiten über die Satz-Kette oder (bisheriger Editor) geschätzte Lücken
    ...(options.satz ? [Satz] : [PageBreaks.configure({ onPageCount: options.onPageCount })]),
    // Manueller Seitenumbruch (Strg+Enter, Toolbar, /-Menü)
    PageBreakNode,
    // Suchen & Ersetzen (lokale Decorations, kollaborationssicher)
    FindReplace.configure({
      onSearchUpdate: options.onSearchUpdate,
    }),
    // Slash Commands
    SlashCommands,
  ]
}
