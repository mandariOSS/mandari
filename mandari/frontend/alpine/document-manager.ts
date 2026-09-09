/**
 * Dokumentenliste (Alpine-Komponente `documentManager`).
 *
 * Ansichtsmodus (Karten/Tabelle) und Ordner-Spalte werden in localStorage gemerkt,
 * Dokumente lassen sich einzeln oder per Mehrfachauswahl in Ordner verschieben, Ordner
 * werden über ein Modal angelegt/bearbeitet. Die URLs kommen aus dem View
 * (`document_manager_config`) per `{{ document_manager_config|json_script:"document-manager-config" }}`.
 *
 * Markup: `templates/work/motions/list.html` und die Partials unter `work/motions/partials/`.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

const CONFIG_ID = 'document-manager-config'
/** Platzhalter-UUID in den Ordner-URLs, wird clientseitig durch die echte ID ersetzt */
const FOLDER_PLACEHOLDER = '00000000-0000-0000-0000-000000000000'
const VIEW_MODE_KEY = 'docViewMode'
const FOLDER_PANEL_KEY = 'docFolderPanel'

export interface DocumentManagerConfig {
  /** ID des aktuell geöffneten Ordners ('' = Wurzel) */
  currentFolderId: string
  urls: {
    folderCreate: string
    folderUpdate: string
    folderDelete: string
    moveToFolder: string
  }
}

export type ViewMode = 'grid' | 'list'
export type FolderModalMode = 'create' | 'edit'

export interface FolderModalState {
  open: boolean
  mode: FolderModalMode
  id: string
  action: string
  deleteAction: string
  name: string
  color: string
  parent: string
  parentName: string
}

function readConfig(): DocumentManagerConfig {
  const config = readJsonScript<DocumentManagerConfig>(CONFIG_ID)
  if (!config) throw new Error(`Dokumentenlisten-Konfiguration #${CONFIG_ID} fehlt`)
  return config
}

function closedFolderModal(): FolderModalState {
  return {
    open: false,
    mode: 'create',
    id: '',
    action: '',
    deleteAction: '',
    name: '',
    color: '',
    parent: '',
    parentName: 'Alle Dokumente',
  }
}

/** Alpine-Komponente: `x-data="documentManager"` */
export const documentManager = defineComponent(() => {
  const config = readConfig()

  return {
    viewMode: 'list' as ViewMode,

    // Teilen-Modal (work/motions/partials/_share_modal.html)
    showShareModal: false,
    shareMotionId: null as string | null,
    shareMotionTitle: '',
    shareVisibility: 'private',
    addUserEmail: '',

    // Ordner-Ablage
    folderPanelOpen: true,
    selectedIds: [] as string[],
    bulkTargetFolder: '',
    folderModal: closedFolderModal(),

    init(): void {
      const stored = localStorage.getItem(VIEW_MODE_KEY)
      if (stored === 'grid' || stored === 'list') {
        this.viewMode = stored
      } else {
        this.viewMode = 'list'
        localStorage.setItem(VIEW_MODE_KEY, 'list')
      }
      this.folderPanelOpen = localStorage.getItem(FOLDER_PANEL_KEY) !== 'closed'
    },

    setViewMode(mode: ViewMode): void {
      this.viewMode = mode
      localStorage.setItem(VIEW_MODE_KEY, mode)
    },

    toggleFolderPanel(): void {
      this.folderPanelOpen = !this.folderPanelOpen
      localStorage.setItem(FOLDER_PANEL_KEY, this.folderPanelOpen ? 'open' : 'closed')
    },

    openFolderCreate(): void {
      this.folderModal = {
        ...closedFolderModal(),
        open: true,
        mode: 'create',
        action: config.urls.folderCreate,
        parent: config.currentFolderId,
      }
    },

    openFolderEdit(id: string, name: string, color: string, parentId: string, parentName: string): void {
      this.folderModal = {
        open: true,
        mode: 'edit',
        id,
        action: config.urls.folderUpdate.replace(FOLDER_PLACEHOLDER, id),
        deleteAction: config.urls.folderDelete.replace(FOLDER_PLACEHOLDER, id),
        name,
        color,
        parent: parentId,
        parentName,
      }
    },

    moveDocuments(ids: string[], folderId: string): void {
      if (!ids.length) return
      const formData = new FormData()
      for (const id of ids) formData.append('motion_ids', id)
      formData.append('folder', folderId || '')
      fetch(config.urls.moveToFolder, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken() },
        body: formData,
      })
        .then((r) => r.json())
        .then(() => window.location.reload())
        .catch(() => window.location.reload())
    },

    openShareModal(motionId: string, title: string, visibility: string): void {
      this.shareMotionId = motionId
      this.shareMotionTitle = title
      this.shareVisibility = visibility
      this.showShareModal = true
    },

    closeShareModal(): void {
      this.showShareModal = false
      this.shareMotionId = null
      this.shareMotionTitle = ''
      this.addUserEmail = ''
    },
  }
})
