/**
 * HTMX-Konfiguration: CSRF, ein zentraler Swap-Hook (Fokus, Ankündigung, Toasts),
 * Bestätigungsdialog für `hx-confirm`, Server-Toasts per `HX-Trigger` und deklarative
 * Nachbearbeitung statt `hx-on`-Inline-JavaScript (#172, CSP ohne unsafe-eval):
 *
 * - `data-autosave="panel"` am automatisch speichernden Formular: vor dessen Anfrage `panel-autosaving`, nach Erfolg
 *   `panel-autosaved`, sonst `panel-autosave-failed` (Detail: Grund und Erklärung, frontend/js/autosave.ts) als
 *   Window-Event (Anzeige in frontend/alpine/autosave-anzeige.ts). Jedes Ereignis trägt die Kennung des Formulars, damit
 *   ein gelungenes Formular den Fehler eines anderen im selben Panel nicht verdeckt. Auch 4xx und eine abgelaufene
 *   Anmeldung gelten als gescheitert (#854); die Pflicht zum zweiten Faktor lädt die Seite nicht neu (die Eingabe
 *   bliebe sonst nicht erhalten). Eine Ablehnung mit neu gezeichnetem Formular (422 mit markierten Feldern) wird
 *   eingezeichnet. Gescheiterte Formulare senden erneut (Ereignis `autosave-erneut` in ihrem `hx-trigger`): auf
 *   „Erneut versuchen“ (`autosave-wiederholen`), nach neuer Anmeldung, sobald die Seite wieder Fokus hat, und wieder
 *   online. Knöpfe im Formular mit eigener Anfrage (Checkliste, Kommentar …) zählen nicht dazu.
 * - `data-after-request="reload|reset|follow-href|notification-read|close-dialog"` nach erfolgreicher Anfrage:
 *   Seite neu laden; Formular zurücksetzen (optional `data-blur="<Selektor>"`); dem eigenen
 *   `href` folgen, sonst `notification:marked-read` auslösen; Benachrichtigung als gelesen
 *   darstellen (Hervorhebung und Punkt im Elternelement `data-parent` entfernen, Button entfernen);
 *   das umgebende `<dialog>` schließen.
 */

import htmx from 'htmx.org'
import { confirmAction } from './alpine/confirm-dialog'
import { showToast } from './alpine/toast'
import {
  type AutosaveFehler,
  type AutosaveFehlerMeldung,
  type AutosaveMeldung,
  autosaveFormular,
  bewerteAutosave,
  formularKennung,
  PFLICHTANGABE_FEHLT,
} from './autosave'
import { csrfToken, csrfTokenAktuell } from './csrf'

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

interface RequestDetail {
  successful?: boolean
  elt?: Element
  xhr?: XMLHttpRequest
}

function requestSource(event: Event): HTMLElement | null {
  const elt = (event as CustomEvent<RequestDetail>).detail?.elt
  const source = elt instanceof HTMLElement ? elt : event.target
  return source instanceof HTMLElement ? source : null
}

/** Formulare, deren letztes automatisches Speichern scheiterte, mit Grund */
const gescheitert = new Map<HTMLElement, AutosaveFehler>()

function fehlertext(xhr: XMLHttpRequest): string {
  if (xhr.status < 400) return ''
  try {
    return xhr.responseText.slice(0, 2000)
  } catch {
    return ''
  }
}

/** Ergebnis eines automatischen Speicherns melden (Erfolg nur, wenn die Antwort wirklich gespeichert hat) */
function meldeAutosave(form: HTMLElement, detail: RequestDetail | undefined): void {
  const xhr = detail?.xhr
  const fehler = xhr
    ? bewerteAutosave({
        status: xhr.status,
        adresse: xhr.responseURL,
        umleitung: xhr.getResponseHeader('HX-Redirect'),
        inhaltstyp: xhr.getResponseHeader('Content-Type') || '',
        text: fehlertext(xhr),
        herkunft: window.location.origin,
      })
    : null
  const name = form.dataset.autosave
  const formular = formularKennung(form)
  if (!fehler && detail?.successful === true) {
    gescheitert.delete(form)
    window.dispatchEvent(new CustomEvent<AutosaveMeldung>(`${name}-autosaved`, { detail: { formular } }))
    return
  }
  // Gescheitert ohne erkennbaren Grund (z. B. Antwort nicht verarbeitbar)
  meldeGescheitert(
    form,
    fehler ?? {
      art: 'server',
      meldung: 'Nicht gespeichert: Fehler auf dem Server. Bitte erneut versuchen.',
      ziel: '',
      zielText: '',
    },
  )
}

function meldeGescheitert(form: HTMLElement, grund: AutosaveFehler): void {
  gescheitert.set(form, grund)
  const detail: AutosaveFehlerMeldung = { ...grund, formular: formularKennung(form) }
  window.dispatchEvent(new CustomEvent<AutosaveFehlerMeldung>(`${form.dataset.autosave}-autosave-failed`, { detail }))
}

/** Gescheiterte automatische Speichervorgänge erneut senden (nur Formulare, die noch auf der Seite sind) */
function autosaveErneut(passt: (fehler: AutosaveFehler) => boolean): void {
  for (const [form, fehler] of [...gescheitert]) {
    if (!form.isConnected) gescheitert.delete(form)
    else if (passt(fehler)) htmx.trigger(form, 'autosave-erneut')
  }
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
    case 'close-dialog':
      el.closest('dialog')?.close()
      break
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
    const form = autosaveFormular(event)
    if (!form?.dataset.autosave) return
    const detail: AutosaveMeldung = { formular: formularKennung(form) }
    window.dispatchEvent(new CustomEvent<AutosaveMeldung>(`${form.dataset.autosave}-autosaving`, { detail }))
  })

  // Die Prüfung im Browser hält die Anfrage an (z. B. Pflichtfeld geleert): nichts gesendet, also nicht gespeichert
  document.body.addEventListener('htmx:validation:halted', (event) => {
    const form = autosaveFormular(event)
    if (form) meldeGescheitert(form, { art: 'abgelehnt', meldung: PFLICHTANGABE_FEHLT, ziel: '', zielText: '' })
  })

  // Abgelehnt mit neu gezeichnetem Formular (422 mit markierten Feldern, z. B. Aufgabentitel nur aus Leerzeichen):
  // einzeichnen, als gescheitert gilt es trotzdem (kein Haken, Hinweis im Panel)
  document.body.addEventListener('htmx:beforeSwap', (event) => {
    const detail = (event as CustomEvent<{ xhr?: XMLHttpRequest; shouldSwap?: boolean }>).detail
    if (detail?.xhr?.status === 422 && autosaveFormular(event)) detail.shouldSwap = true
  })

  // Pflicht zum zweiten Faktor (204 mit HX-Redirect): Beim automatischen Speichern nicht wegnavigieren, sonst wäre
  // die Eingabe verloren; stattdessen erklärt die Anzeige, was zu tun ist
  document.body.addEventListener('htmx:beforeOnLoad', (event) => {
    const xhr = (event as CustomEvent<RequestDetail>).detail?.xhr
    if (autosaveFormular(event) && xhr?.getResponseHeader('HX-Redirect')) event.preventDefault()
  })

  document.body.addEventListener('htmx:afterRequest', (event) => {
    const source = requestSource(event)
    if (!source) return
    const detail = (event as CustomEvent<RequestDetail>).detail
    const successful = detail?.successful === true
    const autosave = autosaveFormular(event)
    if (autosave) meldeAutosave(autosave, detail)
    const after = source.closest<HTMLElement>('[data-after-request]')
    if (after && (successful || after.dataset.afterRequest === 'reload')) afterRequest(after)
  })

  document.body.addEventListener('htmx:configRequest', (event) => {
    const detail = (event as CustomEvent<{ headers: Record<string, string> }>).detail
    // Automatisches Speichern: Token aus dem Cookie, damit es nach einer neuen Anmeldung in einem anderen Tab gilt
    detail.headers['X-CSRFToken'] = autosaveFormular(event) ? csrfTokenAktuell() : csrfToken()
  })

  window.addEventListener('autosave-wiederholen', () => autosaveErneut(() => true))
  // Zurück aus dem Tab mit der Anmeldung bzw. wieder online: ohne Zutun erneut senden
  window.addEventListener('focus', () => autosaveErneut((f) => f.art === 'anmeldung' || f.art === 'verbindung'))
  window.addEventListener('online', () => autosaveErneut((f) => f.art === 'verbindung'))

  // Ein Hook für alles Nachgelagerte: Icons werden per MutationObserver ersetzt,
  // hier bleiben Fokus und die Ankündigung für Screenreader.
  document.body.addEventListener('htmx:afterSettle', (event) => {
    focusSwapped(event.target)
    announce('Inhalt wurde aktualisiert')
  })

  // Server kann Toasts auslösen: HX-Trigger: {"showToast": {"message": "...", "type": "success"}}.
  // htmx löst zu jedem Ereignis zusätzlich die Kebab-Schreibweise aus („show-toast“, Detail = das Objekt) –
  // genau darauf hört der Toast-Container (partials/toasts.html). Ein eigener Listener für „showToast“
  // zeigte jede Meldung doppelt an (Sitzungscockpit, Issue #140).

  // Netzwerk- und Serverfehler sichtbar machen statt still zu scheitern
  // (automatisches Speichern zeigt Fehler selbst am Formular, siehe meldeAutosave)
  document.body.addEventListener('htmx:responseError', (event) => {
    const status = (event as CustomEvent<{ xhr: XMLHttpRequest }>).detail?.xhr?.status
    if (autosaveFormular(event)) return
    if (status && status >= 500) showToast('Der Server hat einen Fehler gemeldet. Bitte erneut versuchen.', 'error')
  })
  document.body.addEventListener('htmx:sendError', (event) => {
    if (autosaveFormular(event)) return
    showToast('Keine Verbindung zum Server.', 'error')
  })

  // hx-confirm auf den eigenen Dialog umleiten (statt nativem Browser-confirm)
  document.body.addEventListener('htmx:confirm', (event) => {
    const detail = (event as CustomEvent<{ question?: string; issueRequest: (skip: boolean) => void }>).detail
    if (!detail.question) return
    event.preventDefault()
    void confirmAction({
      title: 'Sind Sie sicher?',
      message: detail.question,
      confirmText: 'Bestätigen',
      variant: 'danger',
    }).then((ok) => {
      if (ok) detail.issueRequest(true)
    })
  })
}
