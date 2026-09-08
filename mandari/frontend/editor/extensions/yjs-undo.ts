import { Extension } from '@tiptap/core'
import { undo, redo } from '@tiptap/y-tiptap'

export const YjsUndo = Extension.create({
  name: 'yjsUndo',

  addCommands() {
    return {
      // y-tiptap 3: undo/redo dispatchen selbst über den Yjs-UndoManager
      undo:
        () =>
        ({ state }) =>
          undo(state),
      redo:
        () =>
        ({ state }) =>
          redo(state),
    }
  },
})

export default YjsUndo
