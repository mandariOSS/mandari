/**
 * Fraktionssitzung – Detailseite (Alpine-Komponente `factionDetail`).
 *
 * Hält den Zustand der Modale (TOP anlegen/bearbeiten, löschen, Abstimmung, Vorschlag,
 * Teilnehmer, Sitzung bearbeiten) und des Slide-over-Panels, dessen Inhalt per HTMX
 * geladen wird. Die Agenda-Partials öffnen die Modale über Fenster-Events
 * (`open-add-item`, `open-edit-item`, …), die im Template auf die Methoden gemappt sind.
 * Die Konfiguration kommt aus dem View per `{{ detail_config|json_script:"faction-detail-config" }}`.
 *
 * Markup: `templates/work/faction/detail.html` und die `partials/_detail_*`-Partials.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'

export interface FactionDetailConfig {
  /** URL des TOP-Panels; PANEL_ITEM_PLACEHOLDER steht an Stelle der TOP-ID */
  panelUrlTemplate: string
}

/** Platzhalter-UUID, die der View in die Panel-URL einsetzt (siehe FactionMeetingDetailView) */
export const PANEL_ITEM_PLACEHOLDER = '00000000-0000-0000-0000-000000000000'

/** Detail-Objekt der `open-*`-Events aus den Agenda-Partials */
export interface AgendaItemEventDetail {
  id?: string
  item_id?: string
  parent_id?: string
  title?: string
  description?: string
  visibility?: string
}

const CONFIG_ID = 'faction-detail-config'
const ERROR_TOAST_MS = 5000

function errorMessage(xhr: XMLHttpRequest | undefined): string {
  const status = xhr ? xhr.status : 0
  if (status === 403) return 'Keine Berechtigung für diese Aktion.'
  if (status === 400) return xhr?.responseText || 'Ungültige Eingabe.'
  if (status >= 500) return 'Serverfehler — bitte erneut versuchen.'
  return 'Ein Fehler ist aufgetreten.'
}

/** Fehlermeldung als Toast im Nachrichten-Container, sonst als Browser-Hinweis */
function showHtmxError(event: Event): void {
  const detail = (event as CustomEvent<{ xhr?: XMLHttpRequest }>).detail
  const msg = errorMessage(detail?.xhr)
  const container = document.querySelector('.messages-container')
  if (!container) {
    window.alert(msg)
    return
  }
  const toast = document.createElement('div')
  toast.className = 'mb-2 p-3 rounded-xl border text-sm bg-red-50 border-red-200 text-red-700'
  toast.textContent = msg
  container.appendChild(toast)
  window.setTimeout(() => toast.remove(), ERROR_TOAST_MS)
}

export const factionDetail = defineComponent(() => ({
  config: (readJsonScript<FactionDetailConfig>(CONFIG_ID) ?? { panelUrlTemplate: '' }) as FactionDetailConfig,

  // TOP-Modal (anlegen/bearbeiten)
  showItemModal: false,
  itemMode: 'add' as 'add' | 'edit',
  itemVisibility: 'public',
  itemId: '',
  itemTitle: '',
  itemDescription: '',
  parentId: '',

  // Lösch-Modal
  showDeleteModal: false,
  deleteItemId: '',
  deleteItemTitle: '',

  // Abstimmungs-Modal
  showDecisionModal: false,
  decisionItemId: '',
  decisionItemTitle: '',

  // Weitere Modale
  showEditModal: false,
  showProposalModal: false,
  showAddAttendeeModal: false,

  // Slide-over-Panel
  openPanel: false,
  panelItemId: null as string | null,

  init() {
    document.body.addEventListener('htmx:responseError', showHtmxError)
  },

  openItemPanel(itemId: string) {
    this.panelItemId = itemId
    this.openPanel = true
    const url = this.config.panelUrlTemplate.replace(PANEL_ITEM_PLACEHOLDER, itemId)
    void window.htmx.ajax('get', url, { target: '#agenda-panel-content', swap: 'innerHTML' })
  },

  closePanel() {
    this.openPanel = false
    this.panelItemId = null
  },

  openAddItem(detail: AgendaItemEventDetail) {
    this.itemMode = 'add'
    this.itemVisibility = detail.visibility || 'public'
    this.itemId = ''
    this.itemTitle = ''
    this.itemDescription = ''
    this.parentId = ''
    this.showItemModal = true
  },

  openEditItem(detail: AgendaItemEventDetail) {
    this.itemMode = 'edit'
    this.itemId = detail.id ?? ''
    this.itemTitle = detail.title ?? ''
    this.itemDescription = detail.description || ''
    this.parentId = ''
    this.showItemModal = true
  },

  openAddSub(detail: AgendaItemEventDetail) {
    this.itemMode = 'add'
    this.itemVisibility = detail.visibility || 'public'
    this.itemId = ''
    this.itemTitle = ''
    this.itemDescription = ''
    this.parentId = detail.parent_id ?? ''
    this.showItemModal = true
  },

  openDeleteItem(detail: AgendaItemEventDetail) {
    this.deleteItemId = detail.id ?? ''
    this.deleteItemTitle = detail.title ?? ''
    this.showDeleteModal = true
  },

  openDecision(detail: AgendaItemEventDetail) {
    this.decisionItemId = detail.item_id ?? ''
    this.decisionItemTitle = detail.title ?? ''
    this.showDecisionModal = true
  },
}))
