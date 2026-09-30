/**
 * Neues Dokument, Schritt 1 (Alpine-Komponente `createMotion`, #172).
 *
 * Die Vorlagenliste kommt aus dem View per
 * `{{ template_options|json_script:"motion-template-options" }}`, der vorbelegte Dokumenttyp als
 * `data-default-type` am Formular. Wechselt der Typ, springt die Vorlage auf die Standardvorlage
 * des Typs (oder eine typunabhängige).
 *
 * Markup: `templates/work/motions/create.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'

export const TEMPLATE_OPTIONS_ID = 'motion-template-options'

export interface TemplateOption {
  id: string
  name: string
  description: string
  type_name: string | null
  letterhead_name: string | null
  motion_type_id: string | null
  is_default: boolean
}

/** Vorlagen, die zum Dokumenttyp passen (ohne Typ: alle; typunabhängige Vorlagen immer). */
export function templatesForType(templates: TemplateOption[], documentType: string): TemplateOption[] {
  if (!documentType) return templates
  return templates.filter((tpl) => !tpl.motion_type_id || tpl.motion_type_id === documentType)
}

export const createMotion = defineComponent(() => ({
  documentType: '',
  legacyType: 'motion',
  title: '',
  selectedTemplate: '',
  allTemplates: [] as TemplateOption[],

  init() {
    this.allTemplates = readJsonScript<TemplateOption[]>(TEMPLATE_OPTIONS_ID) ?? []
    this.documentType = this.$el.dataset.defaultType ?? ''
    const standard = this.filteredTemplates.find((tpl) => tpl.is_default)
    if (standard) this.selectedTemplate = standard.id
  },

  get filteredTemplates(): TemplateOption[] {
    return templatesForType(this.allTemplates, this.documentType)
  },

  get selectedTemplateInfo(): TemplateOption | null {
    return this.allTemplates.find((tpl) => tpl.id === this.selectedTemplate) ?? null
  },

  filterTemplates() {
    const standard = this.filteredTemplates.find((tpl) => tpl.is_default)
    this.selectedTemplate = standard ? standard.id : ''
  },
}))
