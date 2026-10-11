/**
 * Support (Alpine-Komponente, #172): neues Ticket mit Dateianhängen. Vorher Inline-Skript des Templates.
 *
 * - `ticketForm`: Dateiauswahl (höchstens MAX_FILES, je Datei höchstens MAX_FILE_BYTES).
 *   Markup: `templates/work/support/create.html`. Die frühere Artikelsuche der Wissensdatenbank
 *   entfällt; die Seite verweist auf die Anwenderdokumentation (#589). Im neuen Erscheinungsbild
 *   (`templates/work/support/neu/neu.html`, #951) steuert `ziehen` die Ablagefläche von c-form.datei.
 * - `ticketVerlauf`: Nachrichten eines Tickets, alle fünf Sekunden nachgeladen (hx-trigger am Element). Nach einer
 *   Antwort mit `X-Message-Count` setzt die Komponente `count` in `hx-get` neu, damit der Server bei unverändertem
 *   Stand 204 antwortet. Vorher Inline-Skript in `templates/work/support/detail.html`; Markup:
 *   `templates/work/support/neu/ticket.html` (#951).
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
    /** Datei wird über die Ablagefläche gezogen (c-form.datei mit ``ziehen``) */
    ziehen: false,

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

/** Adresse des Nachladens mit neuem Stand: `count` ersetzen, andere Parameter bleiben. */
export function mitZaehler(adresse: string, anzahl: string, basis: string): string {
  const url = new URL(adresse, basis)
  url.searchParams.set('count', anzahl)
  return url.pathname + url.search
}

export const ticketVerlauf = defineComponent(() => ({
  init() {
    const el = this.$el
    el.addEventListener('htmx:afterRequest', (event) => {
      const xhr = (event as CustomEvent<{ xhr?: XMLHttpRequest }>).detail?.xhr
      const anzahl = xhr?.status === 200 ? xhr.getResponseHeader('X-Message-Count') : null
      const adresse = el.getAttribute('hx-get')
      if (!anzahl || !adresse) return
      el.setAttribute('hx-get', mitZaehler(adresse, anzahl, window.location.origin))
      // htmx liest hx-get beim Einrichten; neu einrichten, damit das nächste Nachladen den neuen Stand sendet
      window.htmx.process(el)
    })
  },
}))
