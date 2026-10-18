/**
 * Tracking-Sidebar des Dokumenteditors (Details / Aufgaben): Zuständigkeit, Themen, Frist,
 * Freigaben und Checkliste.
 *
 * Bewusst unabhängig von Alpine, da das Checklisten-Panel per innerHTML-Swap ersetzt wird.
 * Die Templates rufen `mandariMotionMeta`, `mandariApprovalRequest` und
 * `mandariApprovalDecide` als Globals auf (`_details_sidebar.html`); die Checkliste
 * arbeitet mit `[data-checklist-action]`-Buttons (`_checklist_panel.html`).
 *
 * Aktionen, nach denen die Seite neu lädt (Zuständigkeit, Mitarbeit, Themen, Ordner, Frist, Freigabe anfordern,
 * Zustimmen/Ablehnen), sichern vorher den Text des Editors und laden ohne Rückfrage „Website verlassen?“ (#1018).
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

/** Verbindung zum Editor: vor dem Neuladen sichern und das Neuladen als gewollt markieren (#1018) */
export interface TrackingHooks {
  /** Text, Titel, Dokumenttyp und Briefpapier sichern; false = nicht gesichert (Hinweis zeigt der Editor) */
  sichern: () => Promise<boolean>
  /** Gewolltes Neuladen: keine Rückfrage beim Verlassen */
  verlassen: () => void
}

let installed = false
let hooks: TrackingHooks = { sichern: async () => true, verlassen: () => {} }

async function postForm(url: string, formData: FormData): Promise<TrackingResponse> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken() },
    body: formData,
  })
  return (await response.json()) as TrackingResponse
}

/**
 * Ziel eines Formulars aus dem Attribut: Die Details-Formulare haben ein Feld „action“, das `form.action` verdeckt
 * (der Browser liefert dann das Feld statt der Adresse, die Anfrage ging an „…/[object HTMLInputElement]“, #1018).
 */
function formZiel(form: HTMLFormElement): string {
  return form.getAttribute('action') || window.location.href
}

function errorText(data: TrackingResponse): string {
  return 'Fehler: ' + (data.error || 'Unbekannter Fehler')
}

/**
 * Erst den Editor sichern, dann die Aktion senden und bei Erfolg ohne Rückfrage neu laden. Lässt sich nicht sichern,
 * unterbleibt die Aktion (der Editor zeigt den Hinweis).
 */
async function sichernSendenNeuLaden(
  url: string,
  formData: FormData,
  fehlerText: string,
  erfolg?: (data: TrackingResponse) => void,
): Promise<void> {
  if (!(await hooks.sichern())) return
  try {
    const data = await postForm(url, formData)
    if (data.success) {
      erfolg?.(data)
      hooks.verlassen()
      location.reload()
    } else {
      showToast(errorText(data), 'error')
    }
  } catch {
    showToast(fehlerText, 'error')
  }
}

/** Registriert die Globals und die Checklisten-Delegation (einmalig pro Seite). */
export function installMotionTracking(checklistUrl: string, editorHooks?: TrackingHooks): void {
  if (editorHooks) hooks = editorHooks
  if (installed) return
  installed = true

  // Details-Formulare (set_responsible, set_contributors, set_topics, set_folder, set_due_date)
  window.mandariMotionMeta = (event: Event): boolean => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    // Werte beim Absenden festhalten, gesendet wird nach dem Sichern
    void sichernSendenNeuLaden(formZiel(form), new FormData(form), 'Fehler beim Speichern')
    return false
  }

  // Freigabe anfordern
  window.mandariApprovalRequest = (event: Event): boolean => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    void sichernSendenNeuLaden(formZiel(form), new FormData(form), 'Fehler beim Anfragen der Freigabe', (data) =>
      showToast(data.created ? 'Freigabe angefragt.' : 'Freigabe war bereits angefragt.', 'success'),
    )
    return false
  }

  // Freigabe entscheiden (Freigeben/Ablehnen + Kommentar)
  window.mandariApprovalDecide = (url: string, decision: string): void => {
    const formData = new FormData()
    formData.append('decision', decision)
    const commentEl = document.getElementById('approval-decide-comment') as HTMLTextAreaElement | null
    formData.append('comment', commentEl ? commentEl.value : '')
    void sichernSendenNeuLaden(url, formData, 'Fehler beim Entscheiden')
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
