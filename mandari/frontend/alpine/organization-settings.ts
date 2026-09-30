/**
 * Organisations-Einstellungen (Alpine-Komponenten, #172): Farbwähler, Logo-Vorschau,
 * Rechte-Matrix einer Rolle und Ratsfraktionen. Vorher Inline-Skripte der Templates.
 *
 * Startwerte kommen als Datenattribute am Element mit `x-data`, nie als JavaScript-Literal
 * im Attribut (ein Wert mit Anführungszeichen könnte sonst aus dem String ausbrechen):
 *
 * - `colorPicker`: `data-color` (Startfarbe), `data-fallback-color` (wenn leer).
 *   Markup: `work/organization/settings.html`, `work/organization/role_form.html`.
 * - `logoPreview`: `work/organization/settings.html`.
 * - `permissionsManager`: `data-is-admin="true|false"`; Kategorien über `data-category`
 *   an den Rechte-Checkboxen. Markup: `work/organization/role_form.html`.
 * - `partyManager`: `work/organization/parties.html`.
 */

import { defineComponent } from '../js/alpine/component'

const DEFAULT_COLOR = '#6366f1'

/** Hex-Eingabe bereinigen: nur Hex-Ziffern, höchstens sechs. */
export function cleanHex(value: string): string {
  return value.replace(/[^0-9a-fA-F]/g, '').slice(0, 6)
}

export const colorPicker = defineComponent(() => ({
  color: DEFAULT_COLOR,
  hex: DEFAULT_COLOR.slice(1),

  init() {
    const start = this.$el.dataset.color || this.$el.dataset.fallbackColor || DEFAULT_COLOR
    this.color = start
    this.hex = start.replace('#', '')
    this.$watch('color', (value: string) => {
      this.hex = value.replace('#', '')
    })
  },

  /** Farbfeld als Objekt: Alpine setzt es per CSSOM (kein style-Attribut, CSP-tauglich). */
  get swatchStyle(): Record<string, string> {
    return { backgroundColor: this.color }
  },

  onHexInput() {
    const clean = cleanHex(this.hex)
    this.hex = clean
    if (clean.length === 6) this.color = `#${clean}`
  },
}))

export const logoPreview = defineComponent(() => ({
  previewUrl: '',
  removeLogo: false,

  handleFile(event: Event) {
    const file = (event.target as HTMLInputElement | null)?.files?.[0]
    if (!file) return
    this.removeLogo = false
    this.previewUrl = URL.createObjectURL(file)
  },
}))

export const permissionsManager = defineComponent(() => {
  // Wurzel der Komponente: `this.$el` ist in Methoden, die ein Ausdruck im Template aufruft,
  // das auslösende Element (z. B. die „Alle“-Checkbox) – nur in init() die Wurzel.
  let root: HTMLElement | null = null
  return {
    isAdmin: false,
    openCategories: [] as string[],
    // Zähler als reaktive Abhängigkeit: Die Checkboxen selbst sind DOM-Zustand und für Alpine
    // unsichtbar. Jede Änderung erhöht ihn, damit „Alle“ (Häkchen/unbestimmt) neu berechnet wird.
    revision: 0,

    init() {
      root = this.$el
      this.isAdmin = this.$el.dataset.isAdmin === 'true'
    },

    toggleCategory(category: string) {
      const index = this.openCategories.indexOf(category)
      if (index === -1) this.openCategories.push(category)
      else this.openCategories.splice(index, 1)
    },

    checkboxes(category: string): HTMLInputElement[] {
      const boxes = root ? Array.from(root.querySelectorAll<HTMLInputElement>('input[data-category]')) : []
      return boxes.filter((box) => box.dataset.category === category)
    },

    allChecked(category: string): boolean {
      void this.revision
      const boxes = this.checkboxes(category)
      return boxes.length > 0 && boxes.every((box) => box.checked)
    },

    someChecked(category: string): boolean {
      void this.revision
      return this.checkboxes(category).some((box) => box.checked)
    },

    toggleAll(category: string, checked: boolean) {
      for (const box of this.checkboxes(category)) box.checked = checked
      this.updateState()
    },

    /**
     * „Alle“ einer Kategorie als unbestimmt markieren, wenn nur ein Teil gewählt ist.
     * `indeterminate` ist nur eine DOM-Eigenschaft (kein Attribut); das frühere
     * `:indeterminate.prop` kennt Alpine 3 nicht und setzte bloß ein wirkungsloses Attribut.
     * Aufruf per `x-effect="syncIndeterminate($el, 'kategorie')"`.
     */
    syncIndeterminate(box: HTMLInputElement, category: string) {
      box.indeterminate = this.someChecked(category) && !this.allChecked(category)
    },

    updateState() {
      this.revision++
    },
  }
})

export const partyManager = defineComponent(() => ({
  showAdd: false,
  editing: null as string | null,
}))
