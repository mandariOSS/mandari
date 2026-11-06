/**
 * Insight-Portal (Alpine-Komponenten, #172): Merkliste, Bürgerfrage und Benachrichtigungs-Abo. Vorher
 * Inline-Skripte der Templates. Den Kommunenwechsel übernimmt `kommunen-wahl.ts` (Issue #783).
 *
 * - `merklisteController`: lädt die Karten gemerkter Einträge (`data-entities-url`).
 *   Markup: `templates/pages/merkliste.html`.
 * - `questionForm`: Zeichenzähler, Startwert aus der serverseitig gefüllten Textarea.
 *   Markup: `templates/pages/persons/ask_question.html`.
 * - `kiBewertung`: Daumen hoch/runter unter einer KI-Zusammenfassung (Issue #1221). Das signierte Zeichen der eigenen
 *   Bewertung bleibt je Vorgang und Fassung im lokalen Speicher; damit ändert ein zweiter Klick dieselbe Bewertung.
 *   Kein Cookie. Markup: `templates/partials/paper_summary.html`.
 * - `neighborhoodSubscription`: Nachbarschaft und Stichwort eines Abos, Startwerte als
 *   `data-name`, `data-lat`, `data-lon`, `data-radius`, `data-keyword`, Ortssuche über
 *   `data-autocomplete-url`. Markup: `templates/pages/subscribe.html`,
 *   `templates/pages/subscription_manage.html`, `templates/partials/subscribe_neighborhood_fields.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'
import type { BookmarksStore, BookmarkType } from '../js/stores/bookmarks'

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

// ---- Bewertung einer KI-Zusammenfassung --------------------------------------------------------

interface GespeicherteBewertung {
  hilfreich: boolean
  zeichen: string
}

function lesen(schluessel: string): GespeicherteBewertung | null {
  try {
    const roh = window.localStorage.getItem(schluessel)
    const wert = roh ? (JSON.parse(roh) as GespeicherteBewertung) : null
    return wert && typeof wert.hilfreich === 'boolean' && typeof wert.zeichen === 'string' ? wert : null
  } catch {
    return null
  }
}

export const kiBewertung = defineComponent(() => ({
  wert: null as boolean | null,
  zeichen: '',
  sendet: false,
  meldung: '',
  url: '',
  fassung: '',
  schluessel: '',

  init() {
    this.url = this.$el.dataset.url ?? ''
    this.fassung = this.$el.dataset.fassung ?? ''
    this.schluessel = `mandari:ki-bewertung:${this.$el.dataset.vorgang ?? ''}:${this.fassung}`
    const gespeichert = lesen(this.schluessel)
    if (gespeichert) {
      this.wert = gespeichert.hilfreich
      this.zeichen = gespeichert.zeichen
    }
  },

  async bewerten(hilfreich: boolean) {
    if (this.sendet || !this.url || !this.fassung || this.wert === hilfreich) return
    this.sendet = true
    this.meldung = ''
    const daten = new URLSearchParams({
      wert: hilfreich ? 'hoch' : 'runter',
      fassung: this.fassung,
      zeichen: this.zeichen,
    })
    try {
      const antwort = await fetch(this.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken() },
        body: daten,
        credentials: 'same-origin',
      })
      if (antwort.ok) {
        const ergebnis = (await antwort.json()) as GespeicherteBewertung
        this.wert = ergebnis.hilfreich
        this.zeichen = ergebnis.zeichen
        try {
          window.localStorage.setItem(this.schluessel, JSON.stringify(ergebnis))
        } catch {
          // Ohne lokalen Speicher zählt die Bewertung trotzdem, nur eine spätere Änderung wird eine neue
        }
        this.meldung = 'Danke für deine Rückmeldung.'
      } else if (antwort.status === 409) {
        this.meldung = 'Die Zusammenfassung wurde inzwischen neu erstellt. Bitte lade die Seite neu.'
      } else if (antwort.status === 429) {
        this.meldung = 'Gerade sind es zu viele Bewertungen. Bitte versuche es später erneut.'
      } else {
        this.meldung = 'Die Bewertung konnte nicht gespeichert werden.'
      }
    } catch {
      this.meldung = 'Die Bewertung konnte nicht gespeichert werden.'
    } finally {
      this.sendet = false
    }
  },
}))

// ---- Dokumentzeile: ausgelesener Text --------------------------------------------------------

export const documentText = defineComponent(() => ({
  open: false,

  toggle() {
    this.open = !this.open
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
