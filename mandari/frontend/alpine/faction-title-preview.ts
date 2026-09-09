/**
 * Fraktions-Einstellungen: Live-Vorschau des ersten TOP-Titels (Alpine-Komponente `factionTitlePreview`).
 *
 * Ersetzt die Platzhalter des Titels „mit vorheriger Sitzung“ beim Tippen durch Beispielwerte.
 * Das Eingabefeld trägt `x-ref="titleInput"` und `@input="title = $event.target.value"`,
 * die Vorschau `x-text="preview"`.
 *
 * Markup: `templates/work/organization/partials/_faction_agenda_title_settings.html`.
 */

import { defineComponent } from '../js/alpine/component'

const FALLBACK_TITLE = 'Genehmigung der Tagesordnung und des Protokolls der Sitzung vom {datum_letzte_sitzung}'

/** Beispielwerte der Vorschau (Sitzung am 15.02.2026, vorherige Sitzung am 01.02.2026) */
const EXAMPLE_VALUES: ReadonlyArray<readonly [string, string]> = [
  ['{datum_letzte_sitzung}', '01.02.2026'],
  ['{titel_letzte_sitzung}', 'Fraktionssitzung Februar'],
  ['{nr_letzte_sitzung}', '12'],
  ['{datum}', '15.02.2026'],
  ['{titel}', 'Fraktionssitzung März'],
  ['{nr}', '13'],
]

export function renderTitlePreview(title: string): string {
  let result = title || FALLBACK_TITLE
  for (const [placeholder, value] of EXAMPLE_VALUES) result = result.replace(placeholder, value)
  return result
}

export const factionTitlePreview = defineComponent(() => ({
  title: '',

  init() {
    const input = this.$refs.titleInput as HTMLInputElement | undefined
    this.title = input?.value ?? ''
  },

  get preview(): string {
    return renderTitlePreview(this.title)
  },
}))
