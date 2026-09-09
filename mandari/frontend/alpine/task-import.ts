/**
 * Aufgaben-Import (Alpine-Komponenten `importManager`, `fileImportManager`).
 *
 * `importManager` lädt offene Protokoll-Aufgaben und legt die ausgewählten an,
 * `fileImportManager` schickt eine CSV/JSON/XML-Datei (mit Vorschau per dry_run) an den
 * Server. Beide leben in den Import-Dialogen des Kanban-Boards und lesen die URLs aus
 * derselben Konfiguration wie `kanbanBoard` (`task-board-config`).
 *
 * Markup: `templates/work/tasks/partials/_import_modal.html` und `_file_import_modal.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'
import { readTaskBoardConfig } from './task-board'

export interface ProtocolImportItem {
  id: string
  content: string
  meeting: string
  meeting_date: string
  assignee: string | null
}

export interface FileImportReport {
  total: number
  created: number
  updated: number
  skipped: number
  failed: number
  errors: string[]
  warnings: string[]
  /** true bei Vorschau (dry_run) */
  dry_run_flag: boolean
}

interface FileImportResponse {
  error?: string
  dry_run?: boolean
  report?: Omit<FileImportReport, 'dry_run_flag'>
}

function closeDialog(el: HTMLElement): void {
  el.closest('dialog')?.close()
}

/** Alpine-Komponente: `x-data="importManager"` (Aufgaben aus Protokollen) */
export const importManager = defineComponent(() => {
  const config = readTaskBoardConfig()

  return {
    items: [] as ProtocolImportItem[],
    loading: true,
    selected: [] as string[],

    async init(): Promise<void> {
      await this.loadItems()
    },

    async loadItems(): Promise<void> {
      try {
        const response = await fetch(config.urls.importProtocol)
        const data = (await response.json()) as { items: ProtocolImportItem[] }
        this.items = data.items
      } catch (err) {
        console.error('[Import] Load error:', err)
        this.items = []
      }
      this.loading = false
    },

    async importSelected(): Promise<void> {
      if (this.selected.length === 0) return
      const formData = new FormData()
      for (const id of this.selected) formData.append('entry_ids[]', id)
      try {
        const response = await fetch(config.urls.importProtocol, {
          method: 'POST',
          body: formData,
          headers: { 'X-CSRFToken': csrfToken() },
        })
        if (response.ok) {
          closeDialog(this.$el)
          location.reload()
        }
      } catch (err) {
        console.error('[Import] Import error:', err)
      }
    },
  }
})

/** Alpine-Komponente: `x-data="fileImportManager"` (Aufgaben aus CSV/JSON/XML) */
export const fileImportManager = defineComponent(() => {
  const config = readTaskBoardConfig()

  return {
    busy: false,
    error: '',
    report: null as FileImportReport | null,

    reportHeadline(): string {
      if (!this.report) return ''
      return this.report.dry_run_flag
        ? `Vorschau: ${this.report.total} Zeilen geprüft`
        : `Import abgeschlossen: ${this.report.total} Zeilen verarbeitet`
    },

    async upload(dryRun: boolean): Promise<void> {
      const input = this.$refs.fileInput as HTMLInputElement | undefined
      if (!input?.files || input.files.length === 0) {
        this.error = 'Bitte zuerst eine Datei auswählen.'
        return
      }
      this.busy = true
      this.error = ''

      const formData = new FormData()
      formData.append('file', input.files[0])
      if (dryRun) formData.append('dry_run', '1')

      try {
        const response = await fetch(config.urls.importFile, {
          method: 'POST',
          body: formData,
          headers: { 'X-CSRFToken': csrfToken() },
        })
        const data = (await response.json()) as FileImportResponse

        if (!response.ok || data.error) {
          this.report = null
          this.error = data.error || `Fehler beim Import (HTTP ${response.status}).`
        } else if (data.report) {
          this.report = { ...data.report, dry_run_flag: Boolean(data.dry_run) }
          if (!data.dry_run) {
            window.setTimeout(() => location.reload(), 1500)
          }
        }
      } catch (err) {
        console.error('[FileImport] error:', err)
        this.error = 'Unerwarteter Fehler beim Hochladen.'
      }
      this.busy = false
    },
  }
})
