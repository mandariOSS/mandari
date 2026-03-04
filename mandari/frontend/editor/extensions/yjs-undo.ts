import { Extension } from '@tiptap/core'
import { undo, redo } from '@tiptap/y-tiptap'

export const YjsUndo = Extension.create({
  name: 'yjsUndo',

  addCommands() {
    return {
      undo:
        () =>
        ({ state, dispatch }) =>
          undo(state, dispatch),
      redo:
        () =>
        ({ state, dispatch }) =>
          redo(state, dispatch),
    }
  },
})

export default YjsUndo
