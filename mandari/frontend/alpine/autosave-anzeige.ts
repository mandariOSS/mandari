/**
 * Anzeige für automatisch speichernde htmx-Formulare (#854 Teil 2): „wird gespeichert“, gespeichert (Haken für zwei
 * Sekunden) oder „Nicht gespeichert“ mit Erklärung, Link zur Anmeldung bzw. zum zweiten Faktor oder „Erneut
 * versuchen“. Nie mehr dauerhaft „Speichert …“.
 *
 * Ein Panel kann mehrere solche Formulare haben (TOP: Kopf und Beschreibung). Der Fehler steht deshalb je Formular
 * (Kennung im Ereignis, frontend/js/autosave.ts): Gelingt ein anderes Formular, bleibt der Hinweis stehen; der Haken
 * erscheint erst, wenn kein Formular mehr gescheitert ist.
 *
 * Als Teil einer Komponente (`...autosaveZustand()`, z. B. agendaItemPanel) oder allein (`x-data="autosaveAnzeige"`,
 * Aufgaben-Panel). Die Ereignisse kommen aus frontend/js/htmx-setup.ts und werden im Template gebunden:
 * `@<name>-autosaving.window="markSaving()" @<name>-autosaved.window="markSaved($event.detail)"
 * @<name>-autosave-failed.window="markFailed($event.detail)"`. Erklärung: templates/work/partials/_autosave_hinweis.html.
 */

import { defineComponent } from '../js/alpine/component'
import { type AutosaveFehler, type AutosaveFehlerMeldung, type AutosaveMeldung, EINGABE_PRUEFEN } from '../js/autosave'

const GESPEICHERT_ANZEIGE_MS = 2000

export function autosaveZustand() {
  return {
    saving: false,
    saved: false,
    /** Erklärung zum zuletzt gescheiterten Formular, solange eines gescheitert ist; sonst `null` */
    saveFehler: null as AutosaveFehler | null,
    /** Gescheiterte Formulare (Kennung → Grund), in der Reihenfolge ihres Scheiterns */
    _gescheitert: {} as Record<string, AutosaveFehler>,
    _savedTimer: 0,

    markSaving(): void {
      this.saving = true
      this.saved = false
    },

    markSaved(meldung?: AutosaveMeldung | null): void {
      // Ohne Kennung (z. B. `HX-Trigger` des Servers) zählt nichts: maßgeblich ist die Bewertung in htmx-setup.ts
      const formular = meldung?.formular
      if (!formular) return
      this.saving = false
      delete this._gescheitert[formular]
      const offen = Object.values(this._gescheitert)
      this.saveFehler = offen.length > 0 ? offen[offen.length - 1] : null
      window.clearTimeout(this._savedTimer)
      // Ein anderes Formular ist noch gescheitert: kein Haken
      this.saved = !this.saveFehler
      if (this.saved) {
        this._savedTimer = window.setTimeout(() => {
          this.saved = false
        }, GESPEICHERT_ANZEIGE_MS)
      }
    },

    markFailed(meldung: AutosaveFehlerMeldung | null | undefined): void {
      this.saving = false
      this.saved = false
      const formular = meldung?.formular || ''
      const grund: AutosaveFehler = meldung
        ? { art: meldung.art, meldung: meldung.meldung, ziel: meldung.ziel, zielText: meldung.zielText }
        : { art: 'server', meldung: 'Nicht gespeichert. Bitte erneut versuchen.', ziel: '', zielText: '' }
      // Neu eintragen, damit der jüngste Fehler zuletzt steht
      delete this._gescheitert[formular]
      this._gescheitert[formular] = grund
      this.saveFehler = grund
    },

    /** Gescheiterte automatische Speichervorgänge erneut senden (frontend/js/htmx-setup.ts) */
    autosaveWiederholen(): void {
      window.dispatchEvent(new Event('autosave-wiederholen'))
    },
  }
}

/**
 * Alpine-Komponente: `x-data="autosaveAnzeige"`. Mit `data-autosave-abgelehnt="<Kennung des Formulars>"` hat der
 * Server das Panel neu gezeichnet, weil er das automatische Speichern abgelehnt hat (422, markierte Felder).
 */
export const autosaveAnzeige = defineComponent(() => ({
  ...autosaveZustand(),

  init() {
    const formular = this.$el.dataset.autosaveAbgelehnt
    if (formular) this.markFailed({ art: 'abgelehnt', meldung: EINGABE_PRUEFEN, ziel: '', zielText: '', formular })
  },
}))
