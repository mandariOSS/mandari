/**
 * Stand „Gespeichert“ der Einstellungen › Fraktionssitzungen (Issue #1071, Muster: „Work speichert Änderungen
 * sofort“). Die Abschnitte der Seite speichern als automatisch speichernde htmx-Bereiche (`data-autosave="einst"`,
 * frontend/js/htmx-setup.ts); Anzeige, Fehlerhinweis und erneutes Senden kommen aus frontend/alpine/autosave-anzeige.ts.
 *
 * Hinzu kommt nur: Solange eine Änderung unterwegs oder gescheitert ist, fragt der Browser vor dem Verlassen der Seite
 * nach, damit keine Eingabe verloren geht.
 *
 * Markup: templates/work/organization/neu/fraktion.html
 * (`x-data="einstellungenSofort" @einst-autosaving.window="markSaving()" …`).
 */

import { defineComponent } from '../js/alpine/component'
import { autosaveZustand } from './autosave-anzeige'

export const einstellungenSofort = defineComponent(() => ({
  ...autosaveZustand(),

  init() {
    window.addEventListener('beforeunload', (event: BeforeUnloadEvent) => {
      if (!this.saving && !this.saveFehler) return
      event.preventDefault()
      // Ältere Browser zeigen die Rückfrage nur mit gesetztem returnValue
      event.returnValue = ''
    })
  },
}))
