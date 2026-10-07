/**
 * Deklarative Aktionen statt Inline-Handlern (`onclick=`, `onsubmit=`, `onchange=`).
 *
 * Eine Content-Security-Policy ohne `unsafe-inline` verbietet Inline-Handler. Die
 * Templates beschreiben die Aktion deshalb in `data-*`-Attributen; dieses Modul hängt
 * einmal delegierte Listener an `document` (#172). Übersicht:
 *
 * - `data-confirm="Frage"` auf Formular, Button oder Link: Rückfrage vor Absenden/Klick.
 * - `data-prompt="Frage" data-prompt-field="reason"` auf Formular: Freitext abfragen,
 *   Abbruch verhindert das Absenden, sonst landet die Antwort im benannten Feld.
 * - `data-autosubmit` auf Select/Input: bei Änderung das Formular absenden.
 * - `data-filter-param="status"` auf Select: bei Änderung den Query-Parameter setzen.
 * - `data-href="URL"` auf Zeilen/Karten: Klick navigiert, außer auf innere Links,
 *   Buttons oder Formularfelder.
 * - `data-action="reload|back|print|select|click-target|clear-target|share|confirm-submit|fullscreen"` auf Buttons
 *   (`data-target` als Selektor für click-/clear-target, confirm-submit und fullscreen, `data-share-title` für share).
 *   `fullscreen` schaltet das Element `data-target` in den Vollbildmodus und wieder heraus (Esc beendet ihn auch);
 *   solche Knöpfe stehen mit `hidden` im Markup und erscheinen nur, wenn der Browser Vollbild für Elemente kann.
 *   `confirm-submit` fragt mit dem Bestätigungsdialog (`data-confirm-title`, `data-confirm-message`,
 *   `data-confirm-text`, `data-confirm-variant`) und sendet dann das Formular `data-target` ab.
 * - `data-post="URL"` auf Buttons: bestätigter POST per fetch (`data-confirm-title`,
 *   `data-confirm-message`, `data-confirm-text`, `data-confirm-variant`), danach Neuladen.
 * - `data-submit-to="/pfad/{feld}/"` auf GET-Formularen: Ziel aus Feldwerten bauen; ein
 *   leeres Feld verhindert das Absenden.
 * - `data-submit-once` auf Formularen: nur einmal absenden (Doppelklick), danach die Knöpfe
 *   sperren; nach einer Zurück-Navigation aus dem Seiten-Cache wieder frei.
 * - `data-submit-handler="motion-meta|approval-request"` und
 *   `data-approval-decide="URL" data-decision="approve|reject"`: Brücken zu den globalen
 *   Handlern aus `motion-tracking.ts` (Editor-Seitenleiste).
 */

import { type ConfirmVariant, confirmAction } from './alpine/confirm-dialog'
import { showToast } from './alpine/toast'
import { csrfToken } from './csrf'

const INTERACTIVE = 'a, button, input, select, textarea, label, summary, [contenteditable]'

function closestWithData(target: EventTarget | null, attr: string): HTMLElement | null {
  const el = target instanceof Element ? target : null
  return (el?.closest(`[data-${attr}]`) as HTMLElement | null) ?? null
}

function submitForm(form: HTMLFormElement): void {
  if (typeof form.requestSubmit === 'function') form.requestSubmit()
  else form.submit()
}

async function post(el: HTMLElement): Promise<void> {
  const url = el.dataset.post
  if (!url) return
  if (el.dataset.confirmMessage) {
    const ok = await confirmAction({
      title: el.dataset.confirmTitle ?? 'Bestätigen',
      message: el.dataset.confirmMessage,
      confirmText: el.dataset.confirmText ?? 'OK',
      variant: (el.dataset.confirmVariant as ConfirmVariant | undefined) ?? 'warning',
    })
    if (!ok) return
  }
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
    })
    const data = (await res.json()) as { success?: boolean; error?: string }
    if (data.success) window.location.reload()
    else showToast('Fehler: ' + (data.error || 'Unbekannter Fehler'), 'error')
  } catch (err) {
    showToast('Fehler: ' + (err instanceof Error ? err.message : String(err)), 'error')
  }
}

/** Bestätigungsdialog (`data-confirm-*`), danach das Zielformular absenden. */
async function confirmSubmit(el: HTMLElement, form: HTMLFormElement): Promise<void> {
  const ok = await confirmAction({
    title: el.dataset.confirmTitle ?? 'Bestätigen',
    message: el.dataset.confirmMessage ?? '',
    confirmText: el.dataset.confirmText ?? 'OK',
    variant: (el.dataset.confirmVariant as ConfirmVariant | undefined) ?? 'danger',
  })
  if (ok) form.submit()
}

function runAction(el: HTMLElement, event: Event): void {
  const target = el.dataset.target
  switch (el.dataset.action) {
    case 'reload':
      window.location.reload()
      break
    case 'back':
      window.history.back()
      break
    case 'print':
      window.print()
      break
    case 'select':
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) el.select()
      break
    case 'click-target':
      if (target) (document.querySelector(target) as HTMLElement | null)?.click()
      break
    case 'clear-target': {
      const box = target ? el.closest(target) : null
      if (box) box.innerHTML = ''
      break
    }
    case 'share': {
      const daten = { title: el.dataset.shareTitle ?? document.title, url: window.location.href }
      if (typeof navigator.share === 'function') void navigator.share(daten)
      else void navigator.clipboard?.writeText(daten.url)
      break
    }
    case 'confirm-submit': {
      const form = target ? document.querySelector(target) : null
      if (form instanceof HTMLFormElement) void confirmSubmit(el, form)
      break
    }
    case 'fullscreen': {
      const ziel = target ? document.querySelector(target) : null
      if (document.fullscreenElement) void document.exitFullscreen()
      else if (ziel instanceof HTMLElement) void ziel.requestFullscreen()
      break
    }
    default:
      return
  }
  event.preventDefault()
}

export function handleClick(event: Event): void {
  const target = event.target as Element | null
  if (!target) return

  const confirmEl = target.closest('button[data-confirm], a[data-confirm]') as HTMLElement | null
  if (confirmEl && !window.confirm(confirmEl.dataset.confirm ?? '')) {
    event.preventDefault()
    event.stopImmediatePropagation()
    return
  }

  const actionEl = closestWithData(target, 'action')
  if (actionEl) {
    runAction(actionEl, event)
    return
  }

  const postEl = closestWithData(target, 'post')
  if (postEl) {
    event.preventDefault()
    void post(postEl)
    return
  }

  const decideEl = closestWithData(target, 'approval-decide')
  if (decideEl?.dataset.approvalDecide) {
    event.preventDefault()
    window.mandariApprovalDecide?.(decideEl.dataset.approvalDecide, decideEl.dataset.decision ?? 'approve')
    return
  }

  const rowEl = closestWithData(target, 'href')
  if (rowEl?.dataset.href) {
    // Innere Links, Buttons und Felder behalten ihr eigenes Verhalten
    const inner = target.closest(INTERACTIVE)
    if (inner && rowEl.contains(inner)) return
    window.location.assign(rowEl.dataset.href)
  }
}

export function handleSubmit(event: Event): void {
  const form = event.target
  if (!(form instanceof HTMLFormElement)) return

  if (form.dataset.confirm !== undefined && !window.confirm(form.dataset.confirm)) {
    event.preventDefault()
    return
  }

  if (form.dataset.prompt !== undefined) {
    const antwort = window.prompt(form.dataset.prompt)
    if (antwort === null) {
      event.preventDefault()
      return
    }
    const feld = form.elements.namedItem(form.dataset.promptField ?? '')
    if (feld instanceof HTMLInputElement) feld.value = antwort
  }

  if (form.dataset.submitTo) {
    let leer = false
    const ziel = form.dataset.submitTo.replace(/\{(\w+)\}/g, (_m, name: string) => {
      const feld = form.elements.namedItem(name)
      const wert = feld instanceof HTMLInputElement || feld instanceof HTMLSelectElement ? feld.value : ''
      if (!wert) leer = true
      return encodeURIComponent(wert)
    })
    if (leer) {
      event.preventDefault()
      return
    }
    form.action = ziel
  }

  const handler = form.dataset.submitHandler
  if (handler === 'motion-meta' && window.mandariMotionMeta?.(event) === false) event.preventDefault()
  if (handler === 'approval-request' && window.mandariApprovalRequest?.(event) === false) event.preventDefault()

  if (form.dataset.submitOnce !== undefined && !event.defaultPrevented) {
    if (form.dataset.submitting === 'true') {
      event.preventDefault()
      return
    }
    form.dataset.submitting = 'true'
    // Erst nach dem Absenden sperren – ein gesperrter Knopf gäbe sonst seinen Wert nicht mit
    window.setTimeout(() => {
      for (const button of submitButtons(form)) {
        if (button.disabled) continue
        button.disabled = true
        button.dataset.submitOnceLocked = 'true'
      }
    }, 0)
  }
}

function submitButtons(form: HTMLFormElement): HTMLButtonElement[] {
  return Array.from(form.querySelectorAll<HTMLButtonElement>('button[type="submit"], button:not([type])'))
}

/** Zurück-Navigation aus dem Seiten-Cache: gesperrte `data-submit-once`-Formulare wieder freigeben. */
export function resetSubmitOnce(event: PageTransitionEvent): void {
  if (!event.persisted) return
  for (const form of Array.from(document.querySelectorAll<HTMLFormElement>('form[data-submitting]'))) {
    delete form.dataset.submitting
    for (const button of submitButtons(form)) {
      if (button.dataset.submitOnceLocked === undefined) continue
      button.disabled = false
      delete button.dataset.submitOnceLocked
    }
  }
}

export function handleChange(event: Event): void {
  const el = event.target
  if (!(el instanceof HTMLSelectElement || el instanceof HTMLInputElement)) return

  if (el.dataset.autosubmit !== undefined && el.form) submitForm(el.form)

  const param = el.dataset.filterParam
  if (param) {
    const url = new URL(window.location.href)
    if (el.value) url.searchParams.set(param, el.value)
    else url.searchParams.delete(param)
    window.location.assign(url.toString())
  }
}

const FULLSCREEN = '[data-action="fullscreen"]'

/** Vollbild-Knöpfe nur zeigen, wenn der Browser Elemente im Vollbild zeigen kann; `aria-pressed` folgt dem Zustand. */
export function installFullscreen(root: Document = document): void {
  for (const knopf of Array.from(root.querySelectorAll<HTMLElement>(FULLSCREEN))) {
    knopf.hidden = !root.fullscreenEnabled
  }
  root.addEventListener('fullscreenchange', () => {
    for (const knopf of Array.from(root.querySelectorAll<HTMLElement>(FULLSCREEN))) {
      knopf.setAttribute('aria-pressed', String(root.fullscreenElement !== null))
    }
  })
}

export function installActions(root: Document = document): void {
  installFullscreen(root)
  root.addEventListener('click', handleClick)
  root.addEventListener('submit', handleSubmit)
  root.addEventListener('change', handleChange)
  root.defaultView?.addEventListener('pageshow', resetSubmitOnce)
}
