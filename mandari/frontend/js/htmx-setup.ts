/**
 * HTMX-Konfiguration: CSRF, ein zentraler Swap-Hook (Fokus, Ankündigung, Toasts),
 * Bestätigungsdialog für `hx-confirm` und Server-Toasts per `HX-Trigger`.
 */

import htmx from 'htmx.org'
import { confirmAction } from './alpine/confirm-dialog'
import { type ToastType, showToast } from './alpine/toast'

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

/** CSRF-Token aus dem Meta-Tag des Layouts, sonst aus dem Cookie. */
export function csrfToken(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')
  return meta?.content || readCookie('csrftoken')
}

function announce(text: string): void {
  const live = document.createElement('div')
  live.setAttribute('role', 'status')
  live.setAttribute('aria-live', 'polite')
  live.setAttribute('aria-atomic', 'true')
  live.className = 'sr-only'
  live.textContent = text
  document.body.appendChild(live)
  window.setTimeout(() => live.remove(), 1000)
}

/** Fokus auf das erste `[autofocus]`-Element eines eingetauschten Fragments. */
function focusSwapped(target: EventTarget | null): void {
  if (!(target instanceof Element)) return
  const el = target.matches('[autofocus]') ? target : target.querySelector<HTMLElement>('[autofocus]')
  if (el instanceof HTMLElement && document.activeElement !== el) el.focus({ preventScroll: true })
}

interface ToastDetail {
  message?: string
  type?: ToastType
  value?: { message?: string; type?: ToastType }
}

export function setupHtmx(): void {
  window.htmx = htmx

  document.body.addEventListener('htmx:configRequest', (event) => {
    const detail = (event as CustomEvent<{ headers: Record<string, string> }>).detail
    detail.headers['X-CSRFToken'] = csrfToken()
  })

  // Ein Hook für alles Nachgelagerte: Icons werden per MutationObserver ersetzt,
  // hier bleiben Fokus und die Ankündigung für Screenreader.
  document.body.addEventListener('htmx:afterSettle', (event) => {
    focusSwapped(event.target)
    announce('Inhalt wurde aktualisiert')
  })

  // Server kann Toasts auslösen: HX-Trigger: {"showToast": {"message": "...", "type": "success"}}
  document.body.addEventListener('showToast', (event) => {
    const detail = (event as CustomEvent<ToastDetail>).detail ?? {}
    const payload = detail.value ?? detail
    if (payload.message) showToast(payload.message, payload.type ?? 'info')
  })

  // Netzwerk- und Serverfehler sichtbar machen statt still zu scheitern
  document.body.addEventListener('htmx:responseError', (event) => {
    const status = (event as CustomEvent<{ xhr: XMLHttpRequest }>).detail?.xhr?.status
    if (status && status >= 500) showToast('Der Server hat einen Fehler gemeldet. Bitte erneut versuchen.', 'error')
  })
  document.body.addEventListener('htmx:sendError', () => {
    showToast('Keine Verbindung zum Server.', 'error')
  })

  // hx-confirm auf den eigenen Dialog umleiten (statt nativem Browser-confirm)
  document.body.addEventListener('htmx:confirm', (event) => {
    const detail = (event as CustomEvent<{ question?: string; issueRequest: (skip: boolean) => void }>).detail
    if (!detail.question) return
    event.preventDefault()
    void confirmAction({
      title: 'Bist du sicher?',
      message: detail.question,
      confirmText: 'Bestätigen',
      variant: 'danger',
    }).then((ok) => {
      if (ok) detail.issueRequest(true)
    })
  })
}
