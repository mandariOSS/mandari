/**
 * Support (Alpine-Komponenten, #172): neues Ticket mit Artikelvorschlägen und Dateianhängen,
 * Bewertung eines Hilfe-Artikels. Vorher Inline-Skripte der Templates.
 *
 * - `ticketForm`: `data-search-url` (Artikelsuche) am Formular. Treffer gehen als Fenster-Ereignis
 *   `kb-suggestions` an die Seitenleiste – die frühere Kopplung über `el.__x` stammte aus
 *   Alpine 2 und zeigte in Alpine 3 nie Vorschläge an.
 * - `kbSuggestions`: Seitenleiste, `@kb-suggestions.window="show($event.detail)"`.
 *   Markup beider: `templates/work/support/create.html`.
 * - `feedbackWidget`: `data-url` (POST-Ziel), `data-submitted="true|false"`,
 *   `data-selected="true|false|"` (bisherige Bewertung). Markup: `templates/work/support/kb_article.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'

export const MAX_FILES = 5
export const MAX_FILE_BYTES = 10 * 1024 * 1024
const SEARCH_MIN_LENGTH = 3
const SEARCH_DELAY_MS = 300

export interface ArticleSuggestion {
  id: string
  title: string
  excerpt: string
  url: string
}

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

function publishSuggestions(results: ArticleSuggestion[]): void {
  window.dispatchEvent(new CustomEvent('kb-suggestions', { detail: results }))
}

export const ticketForm = defineComponent(() => {
  // Wurzel und Konfiguration aus init(): In Methoden, die das Template aufruft, ist $el das
  // auslösende Element (Betreff, Dateiauswahl), nicht das Formular.
  let root: HTMLElement | null = null
  return {
    subject: '',
    files: [] as File[],
    searchTimeout: 0,
    searchUrl: '',

    init() {
      root = this.$el
      this.searchUrl = this.$el.dataset.searchUrl ?? ''
    },

    searchArticles() {
      window.clearTimeout(this.searchTimeout)
      const url = this.searchUrl
      if (!url) return
      if (this.subject.length < SEARCH_MIN_LENGTH) {
        publishSuggestions([])
        return
      }
      const query = this.subject
      this.searchTimeout = window.setTimeout(() => {
        fetch(`${url}?q=${encodeURIComponent(query)}`)
          .then((response) => (response.ok ? response.json() : { results: [] }))
          .then((data: { results?: ArticleSuggestion[] }) => publishSuggestions(data.results ?? []))
          .catch(() => publishSuggestions([]))
      }, SEARCH_DELAY_MS)
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

export const kbSuggestions = defineComponent(() => ({
  suggestions: [] as ArticleSuggestion[],

  show(results: ArticleSuggestion[] | null | undefined) {
    this.suggestions = Array.isArray(results) ? results : []
  },
}))

export const feedbackWidget = defineComponent(() => ({
  submitted: false,
  selected: null as boolean | null,
  url: '',

  init() {
    this.url = this.$el.dataset.url ?? ''
    this.submitted = this.$el.dataset.submitted === 'true'
    const selected = this.$el.dataset.selected
    this.selected = selected === 'true' ? true : selected === 'false' ? false : null
  },

  async submitFeedback(isHelpful: boolean) {
    this.selected = isHelpful
    const url = this.url
    if (!url) return
    const body = new FormData()
    body.append('is_helpful', isHelpful ? 'true' : 'false')
    try {
      const response = await fetch(url, {
        method: 'POST',
        body,
        headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
      })
      if (response.ok) this.submitted = true
    } catch (error) {
      console.error('Feedback konnte nicht gesendet werden:', error)
    }
  },
}))
