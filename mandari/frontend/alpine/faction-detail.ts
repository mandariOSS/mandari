/**
 * Fraktionssitzung – Detailseite (Alpine-Komponente `factionDetail`).
 *
 * Hält den Zustand der Modale (TOP anlegen/bearbeiten, Abstimmung, Vorschlag, Teilnehmer,
 * Sitzung bearbeiten) und des Slide-over-Panels, dessen Inhalt per HTMX geladen wird.
 * Die Agenda-Partials öffnen die Modale über Fenster-Events (`open-add-item`, `open-edit-item`, …),
 * die im Template auf die Methoden gemappt sind. Die Löschdialoge für Sitzung und TOP (natives
 * `<dialog>`, Issue #897) laden ihre Folgen beim Öffnen frisch vom Server.
 * Die Konfiguration kommt aus dem View per `{{ detail_config|json_script:"faction-detail-config" }}`.
 *
 * Markup: `templates/work/faction/detail.html` und die `partials/_detail_*`-Partials.
 */

import { defineComponent } from '../js/alpine/component'
import { showToast } from '../js/alpine/toast'
import { autosaveFormular } from '../js/autosave'
import { readJsonScript } from '../js/json-script'

export interface FactionDetailConfig {
  /** Aktions-URL der Sitzung (Löschdialoge laden dort ihre Folgen, Issue #897) */
  actionUrl: string
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
const LOADING_TEXT = 'Folgen werden ermittelt …'

function errorMessage(xhr: XMLHttpRequest | undefined): string {
  const status = xhr ? xhr.status : 0
  const type = xhr?.getResponseHeader('Content-Type') ?? ''
  const plain = type.startsWith('text/plain') ? (xhr?.responseText ?? '').trim() : ''
  if (status === 403) return plain || 'Keine Berechtigung für diese Aktion.'
  if (status === 400) return xhr?.responseText || 'Ungültige Eingabe.'
  if (status >= 500) return 'Serverfehler — bitte erneut versuchen.'
  return 'Ein Fehler ist aufgetreten.'
}

/**
 * Fehlermeldung als Hinweis (Toast-Container beider Rahmen). Nicht beim automatischen Speichern: Das TOP-Panel erklärt
 * den Fehler selbst (frontend/alpine/autosave-anzeige.ts, #854), ein Toast daneben wäre dieselbe Meldung noch einmal.
 */
function showHtmxError(event: Event): void {
  if (autosaveFormular(event)) return
  const detail = (event as CustomEvent<{ xhr?: XMLHttpRequest }>).detail
  showToast(errorMessage(detail?.xhr), 'error')
}

/**
 * Löschdialog öffnen und seinen Inhalt (Folgen, Eingabe) frisch vom Server laden (Issue #897).
 * Lehnt der Server ab (fehlendes Recht), schließt sich der Dialog wieder; die Meldung erscheint als Hinweis.
 */
function openDeleteDialog(dialogId: string, bodyId: string, url: string, values: Record<string, string>): void {
  const dialog = document.getElementById(dialogId)
  const body = document.getElementById(bodyId)
  if (!(dialog instanceof HTMLDialogElement) || !body || !url) return
  const loading = document.createElement('p')
  loading.className = 'text-sm text-gray-500 dark:text-gray-400'
  loading.textContent = LOADING_TEXT
  body.replaceChildren(loading)
  if (!dialog.open) dialog.showModal()
  const closeIfEmpty = () => {
    if (body.contains(loading)) dialog.close()
  }
  void window.htmx.ajax('post', url, { target: body, swap: 'innerHTML', values }).then(closeIfEmpty, closeIfEmpty)
}

export const factionDetail = defineComponent(() => ({
  config: (readJsonScript<FactionDetailConfig>(CONFIG_ID) ?? {
    actionUrl: '',
    panelUrlTemplate: '',
  }) as FactionDetailConfig,

  // TOP-Modal (anlegen/bearbeiten)
  showItemModal: false,
  itemMode: 'add' as 'add' | 'edit',
  itemVisibility: 'public',
  itemId: '',
  itemTitle: '',
  itemDescription: '',
  parentId: '',

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
    openDeleteDialog('delete-item-modal', 'delete-item-body', this.config.actionUrl, {
      action: 'delete_item_preview',
      item_id: detail.id ?? '',
    })
  },

  openDeleteMeeting() {
    openDeleteDialog('delete-meeting-modal', 'delete-meeting-body', this.config.actionUrl, { action: 'delete_preview' })
  },

  openDecision(detail: AgendaItemEventDetail) {
    this.decisionItemId = detail.item_id ?? ''
    this.decisionItemTitle = detail.title ?? ''
    this.showDecisionModal = true
  },
}))
