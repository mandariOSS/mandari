/**
 * Neue Aufgabe: Zuweisung mit Rückfrage (Alpine-Komponente `assignmentHandler`, #172).
 *
 * Wer die Aufgabe einer anderen Person zuweist, bestätigt das in einem Dialog (die Person wird
 * benachrichtigt); Abbrechen stellt die vorige Auswahl wieder her. Die Startauswahl liest die
 * Komponente aus dem serverseitig vorbelegten Select, die eigene Mitgliedschaft aus
 * `data-current-member`.
 *
 * Markup: `templates/work/tasks/create.html`.
 */

import { defineComponent } from '../js/alpine/component'

export const assignmentHandler = defineComponent(() => {
  // Das Select merken: In checkAssignment (aus @change) ist $el das Select selbst, nicht die
  // Wurzel – die frühere Suche per this.$el.querySelector fand nichts und brach mit einer
  // Ausnahme ab, der Dialog erschien nie.
  let select: HTMLSelectElement | null = null
  return {
    selectedAssignee: '',
    previousAssignee: '',
    showConfirmModal: false,
    assigneeName: '',
    currentMemberId: '',

    init() {
      select = this.$el.querySelector<HTMLSelectElement>('select[name="assigned_to"]')
      this.currentMemberId = this.$el.dataset.currentMember ?? ''
      this.selectedAssignee = select?.value ?? ''
      this.previousAssignee = this.selectedAssignee
    },

    checkAssignment() {
      // Sich selbst (leer) oder dieselbe Person zuweisen braucht keine Rückfrage
      if (!this.selectedAssignee || this.selectedAssignee === this.currentMemberId) {
        this.previousAssignee = this.selectedAssignee
        return
      }
      const option = select?.selectedOptions[0]
      this.assigneeName = option?.dataset.name || option?.text.trim() || ''
      this.showConfirmModal = true
    },

    confirmAssignment() {
      this.previousAssignee = this.selectedAssignee
      this.showConfirmModal = false
    },

    cancelAssignment() {
      this.selectedAssignee = this.previousAssignee
      this.showConfirmModal = false
    },
  }
})
