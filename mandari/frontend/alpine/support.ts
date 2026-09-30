/**
 * Support (Alpine-Komponente, #172): neues Ticket mit Dateianhängen. Vorher Inline-Skript des Templates.
 *
 * - `ticketForm`: Dateiauswahl (höchstens MAX_FILES, je Datei höchstens MAX_FILE_BYTES).
 *   Markup: `templates/work/support/create.html`. Die frühere Artikelsuche der Wissensdatenbank
 *   entfällt; die Seite verweist auf die Anwenderdokumentation (#589).
 */

import { defineComponent } from '../js/alpine/component'

export const MAX_FILES = 5
export const MAX_FILE_BYTES = 10 * 1024 * 1024

/** Dateigröße lesbar: B, KB, MB. */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Neue Dateien übernehmen: höchstens MAX_FILES insgesamt, zu große werden übersprungen. */
export function acceptFiles(current: File[], incoming: ArrayLike<File>): File[] {
  const result = [...current]
  for (let i = 0; i < incoming.length && result.length < MAX_FILES; i++) {
    if (incoming[i].size <= MAX_FILE_BYTES) result.push(incoming[i])
  }
  return result
}

export const ticketForm = defineComponent(() => {
  // Wurzel aus init(): In Methoden, die das Template aufruft, ist $el das auslösende Element
  // (Dateiauswahl), nicht das Formular.
  let root: HTMLElement | null = null
  return {
    subject: '',
    files: [] as File[],

    init() {
      root = this.$el
    },

    handleFiles(fileList: FileList | null) {
      if (!fileList) return
      this.files = acceptFiles(this.files, fileList)
      this.updateFileInput()
    },

    removeFile(index: number) {
      this.files.splice(index, 1)
      this.updateFileInput()
    },

    updateFileInput() {
      const input = root?.querySelector<HTMLInputElement>('input[type="file"][name="attachments"]')
      if (!input) return
      const transfer = new DataTransfer()
      for (const file of this.files) transfer.items.add(file)
      input.files = transfer.files
    },

    formatSize(bytes: number): string {
      return formatSize(bytes)
    },
  }
})
