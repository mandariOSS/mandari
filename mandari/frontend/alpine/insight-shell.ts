/**
 * Rahmen des Bürgerportals (Issue #783): Dunkelmodus, Blatt „Mehr“ der mobilen Leiste unten, Kommunenwechsel und
 * Dokumentansicht. Vorher ein Inline-Objekt am `<html>` von `templates/base_insight.html`.
 *
 * Markup: `<html x-data="insightShell" data-active-body="<uuid>">`, Knopf „Mehr“ mit `x-ref="mehrButton"`.
 * Das Blatt ist ein modaler Dialog: Escape schließt es, der Fokus kehrt auf „Mehr“ zurück. Ab `lg` gibt es weder
 * Leiste unten noch Blatt, dort steht die Seitenleiste fest.
 */

import { defineComponent } from '../js/alpine/component'
import { zuletztMerken } from './kommunen-wahl'

/** Nach dieser Zeit ohne Ladeereignis zeigt die Dokumentansicht einen Fehler statt eines Ladekreises. */
export const DOC_TIMEOUT_MS = 30_000

function readDarkMode(): boolean {
  try {
    return localStorage.getItem('darkMode') === 'true'
  } catch {
    return false
  }
}

function storeDarkMode(value: boolean): void {
  try {
    localStorage.setItem('darkMode', String(value))
  } catch {
    // Speicher gesperrt (privater Modus): der Schalter wirkt dann nur auf dieser Seite
  }
}

export const insightShell = defineComponent(() => ({
  darkMode: readDarkMode(),
  mehrOffen: false,
  cityModalOpen: false,
  activeBodyId: '',
  docViewerUrl: '',
  docViewerName: '',
  docViewerMeta: '',
  docViewerLoading: false,
  docViewerError: false,
  _docTimeout: undefined as ReturnType<typeof setTimeout> | undefined,

  init() {
    const data = (this.$el as HTMLElement).dataset
    this.activeBodyId = data.activeBody ?? ''
    this.$watch('darkMode', (value: boolean) => storeDarkMode(value))
    // Besuchte Kommune für „Zuletzt besucht“ im Kommunenwechsel merken (nur im Browser)
    if (data.kommuneUrl && data.kommuneName) {
      zuletztMerken({ name: data.kommuneName, ort: data.kommuneOrt ?? '', url: data.kommuneUrl })
    }
  },

  openMehr() {
    this.mehrOffen = true
  },

  /** Schließt das Blatt „Mehr“ und gibt den Fokus an den Knopf in der Leiste zurück. */
  closeMehr() {
    if (!this.mehrOffen) return
    this.mehrOffen = false
    this.$nextTick(() => (this.$refs.mehrButton as HTMLElement | undefined)?.focus())
  },

  openCityModal() {
    // Aus dem Blatt „Mehr“ heraus: erst das Blatt schließen, sonst liegen zwei Dialoge übereinander. Den Fokus ins
    // Suchfeld setzt der Kommunenwechsel selbst (frontend/alpine/kommunen-wahl.ts).
    this.mehrOffen = false
    this.cityModalOpen = true
  },

  closeCityModal() {
    this.cityModalOpen = false
  },

  openDoc(url: string, name?: string, meta?: string) {
    this.docViewerUrl = url
    this.docViewerName = name || 'Dokument'
    this.docViewerMeta = meta || ''
    this.docViewerLoading = true
    this.docViewerError = false
    clearTimeout(this._docTimeout)
    this._docTimeout = setTimeout(() => {
      if (this.docViewerLoading) {
        this.docViewerLoading = false
        this.docViewerError = true
      }
    }, DOC_TIMEOUT_MS)
  },
}))
