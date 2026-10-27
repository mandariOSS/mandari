import { Extension } from '@tiptap/core'
import { redo, undo } from '@tiptap/y-tiptap'

export const YjsUndo = Extension.create({
  name: 'yjsUndo',

  addCommands() {
    return {
      // y-tiptap 3: undo/redo dispatchen selbst über den Yjs-UndoManager.
      // Ab 3.0.10 liefern sie ohne UndoManager undefined; als false melden.
      undo:
        () =>
        ({ state }) =>
          undo(state) ?? false,
      redo:
        () =>
        ({ state }) =>
          redo(state) ?? false,
    }
  },
})

export default YjsUndo
