/**
 * Rahmen des Bürgerportals (Issue #783): Dunkelmodus, mobiles Menü als Dialog, Kommunenwechsel und
 * Dokumentansicht. Vorher ein Inline-Objekt am `<html>` von `templates/base_insight.html`.
 *
 * Markup: `<html x-data="insightShell" data-active-body="<uuid>">`, Menüknopf mit `x-ref="menuButton"`.
 * Das Menü ist mobil ein modaler Dialog: Escape schließt es, der Fokus kehrt auf den Menüknopf zurück.
 * Ab `lg` steht die Leiste fest; `sidebarOpen` bleibt dort `false`.
 */

import { defineComponent } from '../js/alpine/component'

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
  sidebarOpen: false,
  cityModalOpen: false,
  citySearch: '',
  activeBodyId: '',
  docViewerUrl: '',
  docViewerName: '',
  docViewerMeta: '',
  docViewerLoading: false,
  docViewerError: false,
  _docTimeout: undefined as ReturnType<typeof setTimeout> | undefined,

  init() {
    this.activeBodyId = (this.$el as HTMLElement).dataset.activeBody ?? ''
    this.$watch('darkMode', (value: boolean) => storeDarkMode(value))
  },

  openMenu() {
    this.sidebarOpen = true
  },

  /** Schließt das mobile Menü und gibt den Fokus an den Menüknopf zurück. */
  closeMenu() {
    if (!this.sidebarOpen) return
    this.sidebarOpen = false
    this.$nextTick(() => (this.$refs.menuButton as HTMLElement | undefined)?.focus())
  },

  openCityModal() {
    // Aus dem mobilen Menü heraus: erst das Menü schließen, sonst liegen zwei Dialoge übereinander
    this.sidebarOpen = false
    this.cityModalOpen = true
    this.$nextTick(() => (this.$refs.citySearchInput as HTMLElement | undefined)?.focus())
  },

  closeCityModal() {
    this.cityModalOpen = false
    this.citySearch = ''
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
