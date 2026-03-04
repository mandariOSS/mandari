/**
 * TipTap Indent Extension
 *
 * Adds paragraph/heading indentation via margin-left style.
 * Uses addGlobalAttributes() as recommended by TipTap docs.
 */

import { Extension } from '@tiptap/core'

export interface IndentOptions {
  types: string[]
  minLevel: number
  maxLevel: number
  step: number
}

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    indent: {
      indent: () => ReturnType
      outdent: () => ReturnType
    }
  }
}

export const Indent = Extension.create<IndentOptions>({
  name: 'indent',

  addOptions() {
    return {
      types: ['paragraph', 'heading'],
      minLevel: 0,
      maxLevel: 8,
      step: 40, // pixels per indent level
    }
  },

  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          indent: {
            default: 0,
            parseHTML: (element: HTMLElement) => {
              const marginLeft = element.style.marginLeft
              if (!marginLeft) return 0
              const px = parseInt(marginLeft, 10)
              if (isNaN(px) || px <= 0) return 0
              return Math.round(px / this.options.step)
            },
            renderHTML: (attributes: Record<string, any>) => {
              if (!attributes.indent || attributes.indent <= 0) return {}
              return {
                style: `margin-left: ${attributes.indent * this.options.step}px`,
              }
            },
          },
        },
      },
    ]
  },

  addCommands() {
    return {
      indent:
        () =>
        ({ tr, state, dispatch }) => {
          const { selection } = state
          let changed = false

          state.doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (this.options.types.includes(node.type.name)) {
              const currentIndent = node.attrs.indent || 0
              if (currentIndent < this.options.maxLevel) {
                if (dispatch) {
                  tr.setNodeMarkup(pos, undefined, {
                    ...node.attrs,
                    indent: currentIndent + 1,
                  })
                }
                changed = true
              }
            }
          })

          return changed
        },

      outdent:
        () =>
        ({ tr, state, dispatch }) => {
          const { selection } = state
          let changed = false

          state.doc.nodesBetween(selection.from, selection.to, (node, pos) => {
            if (this.options.types.includes(node.type.name)) {
              const currentIndent = node.attrs.indent || 0
              if (currentIndent > this.options.minLevel) {
                if (dispatch) {
                  tr.setNodeMarkup(pos, undefined, {
                    ...node.attrs,
                    indent: currentIndent - 1,
                  })
                }
                changed = true
              }
            }
          })

          return changed
        },
    }
  },

  addKeyboardShortcuts() {
    return {
      Tab: () => {
        // Only indent if not in a list (lists use sink/lift)
        if (this.editor.isActive('listItem')) return false
        return this.editor.commands.indent()
      },
      'Shift-Tab': () => {
        if (this.editor.isActive('listItem')) return false
        return this.editor.commands.outdent()
      },
    }
  },
})

export default Indent
