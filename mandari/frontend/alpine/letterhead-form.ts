/**
 * Briefkopf-Formular mit Live-Vorschau (Alpine-Komponente `letterheadForm`, #172).
 *
 * Startwerte kommen aus dem View per `{{ letterhead_form|json_script:"letterhead-form-data" }}`,
 * die Vorschau-URL als `data-preview-url` am Element mit `x-data`. Die Vorschau lädt der
 * Server als HTML-Fragment (nur beim generierten Briefkopf).
 *
 * Markup: `templates/work/motions/settings/letterhead_form.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'

export const LETTERHEAD_DATA_ID = 'letterhead-form-data'

export interface LetterheadFormState {
  kind: string
  header_logo_enabled: boolean
  accent_color_enabled: boolean
  sender_line: string
  address_block: string
  footer_text: string
}

export const letterheadForm = defineComponent(() => ({
  kind: 'generated',
  headerLogoEnabled: true,
  accentColorEnabled: true,
  senderLine: '',
  addressBlock: '',
  footerText: '',
  previewUrl: '',

  init() {
    // In init() ist $el die Wurzel; Methoden, die das Template aufruft, sehen dort das auslösende Element
    this.previewUrl = this.$el.dataset.previewUrl ?? ''
    const state = readJsonScript<LetterheadFormState>(LETTERHEAD_DATA_ID)
    if (state) {
      this.kind = state.kind
      this.headerLogoEnabled = state.header_logo_enabled
      this.accentColorEnabled = state.accent_color_enabled
      this.senderLine = state.sender_line
      this.addressBlock = state.address_block
      this.footerText = state.footer_text
    }
    this.$nextTick(() => {
      void this.refreshPreview()
    })
  },

  previewParams(): URLSearchParams {
    return new URLSearchParams({
      header_logo_enabled: this.headerLogoEnabled ? 'on' : '',
      accent_color_enabled: this.accentColorEnabled ? 'on' : '',
      sender_line: this.senderLine,
      address_block: this.addressBlock,
      footer_text: this.footerText,
    })
  },

  async refreshPreview() {
    if (this.kind !== 'generated') return
    const target = document.getElementById('letterhead-preview')
    if (!target || !this.previewUrl) return
    try {
      const response = await fetch(`${this.previewUrl}?${this.previewParams()}`)
      if (response.ok) target.innerHTML = await response.text()
    } catch {
      target.innerHTML = '<p class="text-sm text-red-500">Vorschau konnte nicht geladen werden.</p>'
    }
  },
}))
