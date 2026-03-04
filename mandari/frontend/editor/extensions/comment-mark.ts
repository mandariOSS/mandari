/**
 * TipTap Comment Mark Extension
 *
 * Wraps selected text with a <span data-comment-id="..."> element.
 * Used for inline comment highlights synced with the comments sidebar.
 */

import { Mark, mergeAttributes } from '@tiptap/core'

export interface CommentMarkOptions {
  HTMLAttributes: Record<string, any>
}

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    commentMark: {
      /**
       * Set a comment mark on the current selection.
       */
      setCommentMark: (attributes: { commentId: string }) => ReturnType
      /**
       * Remove a comment mark by ID.
       */
      unsetCommentMark: (commentId: string) => ReturnType
    }
  }
}

export const CommentMark = Mark.create<CommentMarkOptions>({
  name: 'commentMark',

  addOptions() {
    return {
      HTMLAttributes: {},
    }
  },

  addAttributes() {
    return {
      commentId: {
        default: null,
        parseHTML: (element: HTMLElement) => element.getAttribute('data-comment-id'),
        renderHTML: (attributes: Record<string, any>) => {
          if (!attributes.commentId) return {}
          return { 'data-comment-id': attributes.commentId }
        },
      },
      resolved: {
        default: false,
        parseHTML: (element: HTMLElement) => element.getAttribute('data-resolved') === 'true',
        renderHTML: (attributes: Record<string, any>) => {
          if (!attributes.resolved) return {}
          return { 'data-resolved': 'true' }
        },
      },
    }
  },

  parseHTML() {
    return [
      {
        tag: 'span[data-comment-id]',
      },
    ]
  },

  renderHTML({ HTMLAttributes }) {
    const resolved = HTMLAttributes['data-resolved'] === 'true'
    return [
      'span',
      mergeAttributes(this.options.HTMLAttributes, HTMLAttributes, {
        class: resolved
          ? 'comment-mark comment-mark--resolved'
          : 'comment-mark',
      }),
      0,
    ]
  },

  addCommands() {
    return {
      setCommentMark:
        (attributes) =>
        ({ commands }) => {
          return commands.setMark(this.name, attributes)
        },
      unsetCommentMark:
        (commentId) =>
        ({ tr, state, dispatch }) => {
          const { doc } = state
          let found = false

          doc.descendants((node, pos) => {
            node.marks.forEach((mark) => {
              if (
                mark.type.name === this.name &&
                mark.attrs.commentId === commentId
              ) {
                if (dispatch) {
                  tr.removeMark(pos, pos + node.nodeSize, mark)
                }
                found = true
              }
            })
          })

          return found
        },
    }
  },
})

export default CommentMark
