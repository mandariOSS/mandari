/**
 * Tracking-Sidebar des Dokumenteditors (Details / Aufgaben): Zuständigkeit, Themen, Frist,
 * Freigaben und Checkliste.
 *
 * Bewusst unabhängig von Alpine, da das Checklisten-Panel per innerHTML-Swap ersetzt wird.
 * Die Templates rufen `mandariMotionMeta`, `mandariApprovalRequest` und
 * `mandariApprovalDecide` als Globals auf (`_details_sidebar.html`); die Checkliste
 * arbeitet mit `[data-checklist-action]`-Buttons (`_checklist_panel.html`).
 */

import { showToast } from './alpine/toast'
import { csrfToken } from './csrf'

interface TrackingResponse {
  success?: boolean
  error?: string
  created?: boolean
  html?: string
}

declare global {
  interface Window {
    mandariMotionMeta: (event: Event) => boolean
    mandariApprovalRequest: (event: Event) => boolean
    mandariApprovalDecide: (url: string, decision: string) => void
  }
}

let installed = false

async function postForm(url: string, formData: FormData): Promise<TrackingResponse> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken() },
    body: formData,
  })
  return (await response.json()) as TrackingResponse
}

function errorText(data: TrackingResponse): string {
  return 'Fehler: ' + (data.error || 'Unbekannter Fehler')
}

/** Registriert die Globals und die Checklisten-Delegation (einmalig pro Seite). */
export function installMotionTracking(checklistUrl: string): void {
  if (installed) return
  installed = true

  // Details-Formulare (set_responsible, set_contributors, set_topics, set_due_date)
  window.mandariMotionMeta = (event: Event): boolean => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    postForm(form.action, new FormData(form))
      .then((data) => {
        if (data.success) {
          location.reload()
        } else {
          showToast(errorText(data), 'error')
        }
      })
      .catch(() => showToast('Fehler beim Speichern', 'error'))
    return false
  }

  // Freigabe anfordern
  window.mandariApprovalRequest = (event: Event): boolean => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    postForm(form.action, new FormData(form))
      .then((data) => {
        if (data.success) {
          showToast(data.created ? 'Freigabe angefragt.' : 'Freigabe war bereits angefragt.', 'success')
          location.reload()
        } else {
          showToast(errorText(data), 'error')
        }
      })
      .catch(() => showToast('Fehler beim Anfragen der Freigabe', 'error'))
    return false
  }

  // Freigabe entscheiden (Freigeben/Ablehnen + Kommentar)
  window.mandariApprovalDecide = (url: string, decision: string): void => {
    const formData = new FormData()
    formData.append('decision', decision)
    const commentEl = document.getElementById('approval-decide-comment') as HTMLTextAreaElement | null
    formData.append('comment', commentEl ? commentEl.value : '')
    postForm(url, formData)
      .then((data) => {
        if (data.success) {
          location.reload()
        } else {
          showToast(errorText(data), 'error')
        }
      })
      .catch(() => showToast('Fehler beim Entscheiden', 'error'))
  }

  // Checkliste: Event-Delegation auf dem stabilen Wrapper, Panel-Swap per innerHTML
  function checklistAction(formData: FormData): void {
    postForm(checklistUrl, formData)
      .then((data) => {
        if (data.success) {
          const wrapper = document.getElementById('checklist-panel-wrapper')
          if (wrapper && typeof data.html === 'string') {
            wrapper.innerHTML = data.html
          }
        } else {
          showToast(errorText(data), 'error')
        }
      })
      .catch(() => showToast('Fehler bei der Checkliste', 'error'))
  }

  function handleChecklistAdd(): void {
    const input = document.getElementById('checklist-new-title') as HTMLInputElement | null
    if (!input || !input.value.trim()) return
    const formData = new FormData()
    formData.append('action', 'add')
    formData.append('title', input.value.trim())
    checklistAction(formData)
  }

  document.addEventListener('click', (event) => {
    const button = (event.target as Element | null)?.closest<HTMLElement>('[data-checklist-action]')
    if (!button) return
    const action = button.dataset.checklistAction || ''
    if (action === 'add') {
      handleChecklistAdd()
      return
    }
    const formData = new FormData()
    formData.append('action', action)
    formData.append('item_id', button.dataset.itemId || '')
    checklistAction(formData)
  })

  document.addEventListener('keydown', (event) => {
    const target = event.target as HTMLElement | null
    if (event.key === 'Enter' && target && target.id === 'checklist-new-title') {
      event.preventDefault()
      handleChecklistAdd()
    }
  })
}
