/**
 * Insight-Portal (Alpine-Komponenten, #172): Kommunenauswahl, Merkliste, Bürgerfrage und
 * Benachrichtigungs-Abo. Vorher Inline-Skripte der Templates.
 *
 * - `bodySelectApp`: Sofortfilter der Kommune-Karten (`[data-search]` in `x-ref="grid"`).
 *   Markup: `templates/pages/portal/select_body.html`.
 * - `merklisteController`: lädt die Karten gemerkter Einträge (`data-entities-url`).
 *   Markup: `templates/pages/merkliste.html`.
 * - `questionForm`: Zeichenzähler, Startwert aus der serverseitig gefüllten Textarea.
 *   Markup: `templates/pages/persons/ask_question.html`.
 * - `neighborhoodSubscription`: Nachbarschaft und Stichwort eines Abos, Startwerte als
 *   `data-name`, `data-lat`, `data-lon`, `data-radius`, `data-keyword`, Ortssuche über
 *   `data-autocomplete-url`. Markup: `templates/pages/subscribe.html`,
 *   `templates/pages/subscription_manage.html`, `templates/partials/subscribe_neighborhood_fields.html`.
 */

import { defineComponent } from '../js/alpine/component'
import type { BookmarksStore, BookmarkType } from '../js/stores/bookmarks'

// ---- Kommunenauswahl --------------------------------------------------------------------------

export function normalizeQuery(value: string | undefined | null): string {
  return (value || '').toLowerCase().trim()
}

/** Passt jeder Suchbegriff (durch Leerzeichen getrennt) in den Suchtext der Karte? */
export function matchesQuery(haystack: string | undefined, query: string): boolean {
  const q = normalizeQuery(query)
  if (!q) return true
  const text = haystack ?? ''
  return q.split(/\s+/).every((term) => text.includes(term))
}

export const bodySelectApp = defineComponent(() => ({
  q: '',
  visibleCount: 0,

  init() {
    // x-ref="grid" steht erst nach dem Initialisieren der Kinder bereit
    this.$nextTick(() => this.updateCount())
    this.$watch('q', () => this.updateCount())
  },

  matches(haystack: string | undefined): boolean {
    return matchesQuery(haystack, this.q)
  },

  updateCount() {
    const grid = this.$refs.grid as HTMLElement | undefined
    if (!grid) {
      this.visibleCount = 0
      return
    }
    const cards = Array.from(grid.querySelectorAll<HTMLElement>('[data-search]'))
    this.visibleCount = cards.filter((card) => this.matches(card.dataset.search)).length
  },
}))

// ---- Merkliste --------------------------------------------------------------------------------

export const merklisteController = defineComponent(() => ({
  loadedTypes: {} as Record<string, string>,
  entitiesUrl: '',

  init() {
    this.entitiesUrl = this.$el.dataset.entitiesUrl ?? ''
  },

  loadEntities(type: BookmarkType, container: HTMLElement | undefined) {
    if (!container) return
    const store = (this.$store as unknown as { bookmarks?: BookmarksStore }).bookmarks
    const ids = store?._data[type] ?? []
    if (ids.length === 0) {
      container.innerHTML = ''
      return
    }
    // Kopie sortieren: Die Liste im Store bleibt in Merk-Reihenfolge und löst keinen zweiten Lauf aus
    const key = `${type}:${[...ids].sort().join(',')}`
    if (this.loadedTypes[type] === key) return
    this.loadedTypes[type] = key
    const url = this.entitiesUrl
    if (!url) return
    const params = new URLSearchParams({ type, ids: ids.join(',') })
    fetch(`${url}?${params}`)
      .then((response) => response.text())
      .then((html) => {
        container.innerHTML = html
      })
      .catch(() => {
        this.loadedTypes[type] = ''
      })
  },
}))

// ---- Bürgerfrage -----------------------------------------------------------------------------

export const questionForm = defineComponent(() => ({
  questionText: '',

  init() {
    this.questionText = this.$el.querySelector<HTMLTextAreaElement>('textarea[data-question-text]')?.value ?? ''
  },
}))

// ---- Benachrichtigungs-Abo -------------------------------------------------------------------

export interface PlaceSuggestion {
  name: string
  lat: number | string
  lon: number | string
}

export const neighborhoodSubscription = defineComponent(() => ({
  neighborhoodName: '',
  neighborhoodLat: '' as string | number,
  neighborhoodLon: '' as string | number,
  neighborhoodRadius: 500,
  keyword: '',
  suggestions: [] as PlaceSuggestion[],
  autocompleteUrl: '',

  init() {
    const data = this.$el.dataset
    this.autocompleteUrl = data.autocompleteUrl ?? ''
    this.neighborhoodName = data.name ?? ''
    this.neighborhoodLat = data.lat ?? ''
    this.neighborhoodLon = data.lon ?? ''
    this.neighborhoodRadius = Number.parseInt(data.radius ?? '', 10) || 500
    this.keyword = data.keyword ?? ''
  },

  async fetchSuggestions() {
    const url = this.autocompleteUrl
    if (!url || this.neighborhoodName.length < 2) {
      this.suggestions = []
      return
    }
    try {
      const response = await fetch(`${url}?q=${encodeURIComponent(this.neighborhoodName)}`)
      this.suggestions = response.ok ? ((await response.json()) as PlaceSuggestion[]) : []
    } catch {
      this.suggestions = []
    }
  },

  selectSuggestion(suggestion: PlaceSuggestion) {
    this.neighborhoodName = suggestion.name
    this.neighborhoodLat = suggestion.lat
    this.neighborhoodLon = suggestion.lon
    this.suggestions = []
  },

  clearNeighborhood() {
    this.neighborhoodName = ''
    this.neighborhoodLat = ''
    this.neighborhoodLon = ''
    this.suggestions = []
  },
}))
