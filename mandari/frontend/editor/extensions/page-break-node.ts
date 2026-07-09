/**
 * Manueller Seitenumbruch — atomarer Block-Node.
 *
 * Rendert als <div data-page-break class="page-break"> und wird von der
 * PageBreaks-Pagination als erzwungene Seitengrenze behandelt. Im
 * PDF-Export (xhtml2pdf) erzeugt die Klasse "page-break" einen echten
 * Umbruch (page-break-after), im DOCX-Export einen add_page_break().
 */

import { Node, mergeAttributes } from '@tiptap/core'

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    pageBreakNode: {
      /** Manuellen Seitenumbruch an der Cursorposition einfügen. */
      setPageBreak: () => ReturnType
    }
  }
}

export const PageBreakNode = Node.create({
  name: 'pageBreak',

  group: 'block',
  atom: true,
  selectable: true,

  parseHTML() {
    return [{ tag: 'div[data-page-break]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return [
      'div',
      mergeAttributes(HTMLAttributes, {
        'data-page-break': 'true',
        class: 'page-break',
      }),
    ]
  },

  addCommands() {
    return {
      setPageBreak:
        () =>
        ({ chain }) =>
          chain()
            .insertContent({ type: this.name })
            // Nach dem Umbruch immer einen Absatz zum Weiterschreiben anbieten
            .command(({ tr, dispatch }) => {
              if (dispatch) {
                const { $to } = tr.selection
                if ($to.nodeAfter === null) {
                  const paragraph = tr.doc.type.schema.nodes.paragraph
                  if (paragraph) tr.insert($to.pos, paragraph.create())
                }
              }
              return true
            })
            .run(),
    }
  },

  addKeyboardShortcuts() {
    return {
      // Word-Konvention: Strg+Enter = Seitenumbruch
      'Mod-Enter': () => this.editor.commands.setPageBreak(),
    }
  },
})

export default PageBreakNode
