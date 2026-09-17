/**
 * HTMX-Konfiguration: CSRF, ein zentraler Swap-Hook (Fokus, Ankündigung, Toasts),
 * Bestätigungsdialog für `hx-confirm`, Server-Toasts per `HX-Trigger` und deklarative
 * Nachbearbeitung statt `hx-on`-Inline-JavaScript (#172, CSP ohne unsafe-eval):
 *
 * - `data-autosave="panel"`: vor der Anfrage `panel-autosaving`, nach Erfolg `panel-autosaved`
 *   als Window-Event (Anzeige „wird gespeichert …“ in Alpine-Komponenten).
 * - `data-after-request="reload|reset|follow-href|notification-read"` nach erfolgreicher Anfrage:
 *   Seite neu laden; Formular zurücksetzen (optional `data-blur="<Selektor>"`); dem eigenen
 *   `href` folgen, sonst `notification:marked-read` auslösen; Benachrichtigung als gelesen
 *   darstellen (Hervorhebung und Punkt im Elternelement `data-parent` entfernen, Button entfernen).
 */

import htmx from 'htmx.org'
import { confirmAction } from './alpine/confirm-dialog'
import { type ToastType, showToast } from './alpine/toast'
import { csrfToken } from './csrf'

export { csrfToken }

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

interface RequestDetail {
  successful?: boolean
  elt?: Element
}

function requestSource(event: Event): HTMLElement | null {
  const elt = (event as CustomEvent<RequestDetail>).detail?.elt
  const source = elt instanceof HTMLElement ? elt : event.target
  return source instanceof HTMLElement ? source : null
}

function afterRequest(el: HTMLElement): void {
  switch (el.dataset.afterRequest) {
    case 'reload':
      window.location.reload()
      break
    case 'reset': {
      if (el instanceof HTMLFormElement) el.reset()
      const blur = el.dataset.blur ? el.querySelector<HTMLElement>(el.dataset.blur) : null
      blur?.blur()
      break
    }
    case 'follow-href': {
      const href = el.getAttribute('href')
      if (href && href !== '#') window.location.href = href
      else window.dispatchEvent(new CustomEvent('notification:marked-read'))
      break
    }
    case 'notification-read': {
      const parent = el.closest<HTMLElement>(el.dataset.parent ?? '.p-4')
      parent?.classList.remove('bg-primary-50/50', 'dark:bg-primary-900/10')
      parent?.querySelector('.inline-block.w-2.h-2')?.remove()
      el.remove()
      break
    }
    default:
      break
  }
}

export function setupHtmx(): void {
  window.htmx = htmx
  // Kein Inline-JavaScript in hx-on/hx-vals/Trigger-Filtern: Voraussetzung für eine CSP ohne
  // unsafe-eval (#172). Nachbearbeitung läuft über die data-*-Attribute oben.
  htmx.config.allowEval = false
  htmx.config.selfRequestsOnly = true

  document.body.addEventListener('htmx:beforeRequest', (event) => {
    const source = requestSource(event)
    const name = source?.closest<HTMLElement>('[data-autosave]')?.dataset.autosave
    if (name) window.dispatchEvent(new Event(`${name}-autosaving`))
  })

  document.body.addEventListener('htmx:afterRequest', (event) => {
    const source = requestSource(event)
    if (!source) return
    const successful = (event as CustomEvent<RequestDetail>).detail?.successful === true
    const autosave = source.closest<HTMLElement>('[data-autosave]')
    if (autosave && successful) window.dispatchEvent(new Event(`${autosave.dataset.autosave}-autosaved`))
    const after = source.closest<HTMLElement>('[data-after-request]')
    if (after && (successful || after.dataset.afterRequest === 'reload')) afterRequest(after)
  })

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
