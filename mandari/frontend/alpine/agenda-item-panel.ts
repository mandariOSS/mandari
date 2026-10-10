/**
 * TOP-Panel der Fraktionssitzung (Alpine-Komponente `agendaItemPanel`).
 *
 * Slide-over für einen Tagesordnungspunkt, per HTMX in `#agenda-panel-content` geladen:
 * Autosave-Anzeige (mit Fehlern, frontend/alpine/autosave-anzeige.ts), ein-/ausklappbare Formulare (Beschluss, Protokolleintrag, Aufgabe,
 * Antrag, Link) und die Live-Suche nach RIS-Vorlagen. Die Konfiguration (Aktions-URL)
 * kommt aus dem View per `{{ panel_config|json_script:"agenda-item-panel-config" }}` und
 * wird in init() gelesen, weil das Partial nach jeder Aktion neu geladen wird.
 *
 * Markup: `templates/work/faction/_agenda_item_panel.html` und die
 * `partials/_agenda_item_panel_*`-Partials.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'
import { autosaveZustand } from './autosave-anzeige'

export interface AgendaItemPanelConfig {
  /** POST-Ziel der Panel-Aktionen (u. a. `action=search_papers`) */
  actionUrl: string
}

/** Treffer der Vorlagensuche (Antwort von `search_papers`) */
export interface PaperSearchResult {
  id: string
  name: string
  reference: string
  paper_type: string
  date: string
}

const CONFIG_ID = 'agenda-item-panel-config'
const MIN_QUERY_LENGTH = 2

export const agendaItemPanel = defineComponent(() => ({
  actionUrl: '',

  // Autosave-Anzeige: saving, saved, saveFehler; markSaving/markSaved/markFailed
  ...autosaveZustand(),

  // Ein-/ausklappbare Formulare
  showDecisionForm: false,
  showEntryForm: false,
  showTaskForm: false,
  showLinkForm: false,
  showMotionSearch: false,
  showPaperSearch: false,
  editingEntry: null as string | null,

  // Vorlagensuche
  paperSearchQuery: '',
  paperSearchResults: [] as PaperSearchResult[],
  paperSearching: false,

  init() {
    this.actionUrl = readJsonScript<AgendaItemPanelConfig>(CONFIG_ID)?.actionUrl ?? ''
  },

  togglePaperSearch() {
    this.showPaperSearch = !this.showPaperSearch
    if (this.showPaperSearch) this.$nextTick(() => this.$refs.paperSearchInput?.focus())
  },

  closePaperSearch() {
    this.showPaperSearch = false
    this.paperSearchQuery = ''
    this.paperSearchResults = []
  },

  searchPapers() {
    if (this.paperSearchQuery.length < MIN_QUERY_LENGTH) {
      this.paperSearchResults = []
      return
    }
    this.paperSearching = true
    fetch(this.actionUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken() },
      body: `action=search_papers&q=${encodeURIComponent(this.paperSearchQuery)}`,
    })
      .then((response) => response.json())
      .then((data: { results: PaperSearchResult[] }) => {
        this.paperSearchResults = data.results
        this.paperSearching = false
      })
      .catch(() => {
        this.paperSearching = false
      })
  },
}))
