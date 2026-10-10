/**
 * Anzeige für automatisch speichernde htmx-Formulare (#854 Teil 2): „wird gespeichert“, gespeichert (Haken für zwei
 * Sekunden) oder „Nicht gespeichert“ mit Erklärung, Link zur Anmeldung bzw. zum zweiten Faktor oder „Erneut
 * versuchen“. Nie mehr dauerhaft „Speichert …“.
 *
 * Als Teil einer Komponente (`...autosaveZustand()`, z. B. agendaItemPanel) oder allein (`x-data="autosaveAnzeige"`,
 * Aufgaben-Panel). Die Ereignisse kommen aus frontend/js/htmx-setup.ts und werden im Template gebunden:
 * `@<name>-autosaving.window="markSaving()" @<name>-autosaved.window="markSaved()"
 * @<name>-autosave-failed.window="markFailed($event.detail)"`. Erklärung: templates/work/partials/_autosave_hinweis.html.
 */

import { defineComponent } from '../js/alpine/component'
import type { AutosaveFehler } from '../js/autosave'

const GESPEICHERT_ANZEIGE_MS = 2000

export function autosaveZustand() {
  return {
    saving: false,
    saved: false,
    /** Letztes automatisches Speichern gescheitert: Grund und Erklärung; `null`, sobald wieder gespeichert */
    saveFehler: null as AutosaveFehler | null,
    _savedTimer: 0,

    markSaving(): void {
      this.saving = true
      this.saved = false
    },

    markSaved(): void {
      this.saving = false
      this.saveFehler = null
      this.saved = true
      window.clearTimeout(this._savedTimer)
      this._savedTimer = window.setTimeout(() => {
        this.saved = false
      }, GESPEICHERT_ANZEIGE_MS)
    },

    markFailed(fehler: AutosaveFehler | null | undefined): void {
      this.saving = false
      this.saved = false
      this.saveFehler = fehler || {
        art: 'server',
        meldung: 'Nicht gespeichert. Bitte erneut versuchen.',
        ziel: '',
        zielText: '',
      }
    },

    /** Gescheiterte automatische Speichervorgänge erneut senden (frontend/js/htmx-setup.ts) */
    autosaveWiederholen(): void {
      window.dispatchEvent(new Event('autosave-wiederholen'))
    },
  }
}

/** Alpine-Komponente: `x-data="autosaveAnzeige"` */
export const autosaveAnzeige = defineComponent(() => autosaveZustand())
