/**
 * Mein Profil (Alpine-Komponenten, #172): Änderungsanträge und DSGVO-Datenexport.
 * Vorher Inline-Skripte der Templates.
 *
 * - `changeRequestForm`: Art des Antrags. Markup: `templates/work/profile/change_requests.html`.
 * - `dataExport`: Exporte aus `{{ exports_data|json_script:"data-exports" }}`; URL-Vorlagen als
 *   `data-status-url` und `data-delete-url` mit der Platzhalter-UUID; `data-active="true"`, wenn
 *   gerade ein Export entsteht (dann wird alle drei Sekunden der Status abgefragt).
 *   Markup: `templates/work/profile/data_privacy.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

export const EXPORTS_DATA_ID = 'data-exports'
export const EXPORT_PLACEHOLDER = '00000000-0000-0000-0000-000000000000'
const POLL_INTERVAL_MS = 3000

export interface DataExportEntry {
  id: string
  status: string
  format: string
  file_size: number | null
  file_size_human: string
  is_ready: boolean
  is_in_progress: boolean
  download_url: string | null
  error_message: string
  created_at: string | null
  completed_at: string | null
}

export function isRunning(entry: Pick<DataExportEntry, 'status'>): boolean {
  return entry.status === 'pending' || entry.status === 'processing'
}

/** Zeitpunkt deutsch mit Datum und Uhrzeit, leer ohne Wert. */
export function formatExportDate(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString('de-DE', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export const changeRequestForm = defineComponent(() => ({
  requestType: '',
}))

export const dataExport = defineComponent(() => ({
  format: 'json',
  exports: [] as DataExportEntry[],
  hasActiveExport: false,
  pollTimer: 0,
  statusUrl: '',
  deleteUrl: '',

  init() {
    // URL-Vorlagen in init() lesen: deleteExport läuft aus @click, dort ist $el der Knopf
    this.statusUrl = this.$el.dataset.statusUrl ?? ''
    this.deleteUrl = this.$el.dataset.deleteUrl ?? ''
    this.exports = readJsonScript<DataExportEntry[]>(EXPORTS_DATA_ID) ?? []
    this.hasActiveExport = this.$el.dataset.active === 'true'
    if (this.hasActiveExport) this.startPolling()
  },

  destroy() {
    this.stopPolling()
  },

  urlFor(kind: 'status' | 'delete', id: string): string {
    const template = kind === 'status' ? this.statusUrl : this.deleteUrl
    return template.replace(EXPORT_PLACEHOLDER, id)
  },

  startPolling() {
    if (this.pollTimer) return
    this.pollTimer = window.setInterval(() => {
      void this.pollStatus()
    }, POLL_INTERVAL_MS)
  },

  stopPolling() {
    if (!this.pollTimer) return
    window.clearInterval(this.pollTimer)
    this.pollTimer = 0
  },

  async pollStatus() {
    let stillActive = false
    for (const entry of this.exports) {
      if (!isRunning(entry)) continue
      try {
        const response = await fetch(this.urlFor('status', entry.id))
        if (!response.ok) continue
        const data = (await response.json()) as DataExportEntry
        Object.assign(entry, data)
        if (data.is_in_progress) stillActive = true
      } catch {
        // Netzwerkfehler: Eintrag bleibt unverändert (Verhalten wie vor der Auslagerung)
      }
    }
    this.hasActiveExport = stillActive
    if (!stillActive) this.stopPolling()
  },

  async deleteExport(id: string) {
    try {
      const response = await fetch(this.urlFor('delete', id), {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
      })
      if (response.ok) {
        this.exports = this.exports.filter((entry) => entry.id !== id)
        this.hasActiveExport = this.exports.some(isRunning)
      }
    } catch {
      window.location.reload()
    }
  },

  formatDate(iso: string | null): string {
    return formatExportDate(iso)
  },
}))
