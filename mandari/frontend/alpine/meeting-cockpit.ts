/**
 * Sitzungscockpit des Session RIS (Issue #140): Stand live halten.
 *
 * Markup: `templates/session/cockpit/page.html` (`x-data="meetingCockpit"`, `data-ws-path`). Der Stand
 * `#cockpit-stand` ist ein HTMX-Fragment mit `hx-trigger="cockpit:refresh"`; diese Komponente löst das
 * Ereignis aus:
 *
 * - bei jedem Hinweis über den WebSocket (`{"type": "stand"}`) – der Socket trägt bewusst keinen Inhalt,
 *   jede Ansicht holt ihren Stand mit den eigenen Rechten ab; dicht folgende Hinweise ergeben einen Abruf;
 * - ohne Verbindung alle zwei Sekunden (Polling-Rückfall), mit Verbindung zur Sicherheit alle 30 Sekunden;
 * - nach einer abgewiesenen Aktion (`cockpit:nachladen` aus dem `HX-Trigger` der Antwort).
 *
 * Der Server antwortet mit 204, solange sich nichts geändert hat (`?v=` im Fragment).
 *
 * Eingaben gehen nicht verloren: Ein Formular im Stand (`data-cockpit-form`) gilt nach der ersten Eingabe als
 * ungespeichert. Tauscht HTMX den Stand aus – nach einem Hinweis, im Polling oder nach einer anderen Aktion –,
 * übernimmt das neue Formular gleichen Schlüssels die Eingaben (Stimmenzahlen, Ergebnis, Vermerk, Auswahl).
 * Nur das gerade abgeschickte Formular erscheint frisch; weist der Server die Aktion ab, tauscht er nicht
 * (`HX-Reswap: none`) und die Eingaben bleiben stehen. Während jemand in einem Textfeld tippt, wartet die
 * Aktualisierung bis zum Verlassen des Feldes.
 */

import { defineComponent } from '../js/alpine/component'

export const POLL_MS = 2000
export const SAFETY_POLL_MS = 30000
/** Hinweise, die so dicht folgen (eine Aktion speichert mehrere Objekte), ergeben einen Abruf */
export const COALESCE_MS = 100
const RECONNECT_MS = [1000, 2000, 5000, 10000, 30000]
const STATE_ID = 'cockpit-stand'
/** Ereignis aus dem `HX-Trigger` einer abgewiesenen Aktion */
const RELOAD_EVENT = 'cockpit:nachladen'

export type LiveMode = 'connecting' | 'ws' | 'poll'

/** Eingabe eines Formularfelds (Kontrollkästchen und Optionsfelder je Wert). */
export interface FieldInput {
  name: string
  value: string
  checked: boolean
  check: boolean
}

/** Ungespeicherte Eingaben je Formularschlüssel (`data-cockpit-form`). */
export type UnsavedInput = Map<string, FieldInput[]>

/** WebSocket-Adresse zum Pfad aus `data-ws-path` (gleicher Host, ws/wss wie die Seite). */
export function socketUrl(path: string, location: Pick<Location, 'protocol' | 'host'>): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${path}`
}

/** Tippt jemand gerade in einem Feld des Stands? Dann später aktualisieren. */
export function isEditing(container: Element | null, active: Element | null): boolean {
  if (!container || !active || !container.contains(active)) return false
  return active.matches('input:not([type=hidden]):not([type=radio]):not([type=checkbox]), select, textarea')
}

/** Eingabe in einem Formular des Stands: Formular als ungespeichert markieren. */
export function markUnsaved(target: EventTarget | null, container: Element | null): void {
  if (!(target instanceof Element) || !container?.contains(target)) return
  if (target instanceof HTMLInputElement && target.type === 'hidden') return
  const form = target.closest<HTMLFormElement>('form[data-cockpit-form]')
  if (form && container.contains(form)) form.dataset.cockpitGeaendert = '1'
}

function fieldsOf(form: HTMLFormElement): FieldInput[] {
  const fields: FieldInput[] = []
  for (const element of Array.from(form.elements)) {
    if (element instanceof HTMLInputElement) {
      if (!element.name || ['hidden', 'submit', 'button', 'reset', 'file'].includes(element.type)) continue
      const check = element.type === 'radio' || element.type === 'checkbox'
      fields.push({ name: element.name, value: element.value, checked: element.checked, check })
    } else if (element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement) {
      if (element.name) fields.push({ name: element.name, value: element.value, checked: false, check: false })
    }
  }
  return fields
}

/** Eingaben aller ungespeicherten Formulare im Stand – außer dem gerade abgeschickten (`skip`). */
export function collectUnsaved(container: ParentNode, skip: string | null): UnsavedInput {
  const unsaved: UnsavedInput = new Map()
  for (const form of Array.from(
    container.querySelectorAll<HTMLFormElement>('form[data-cockpit-form][data-cockpit-geaendert]'),
  )) {
    const key = form.dataset.cockpitForm
    if (key && key !== skip) unsaved.set(key, fieldsOf(form))
  }
  return unsaved
}

/** Eingaben in die neuen Formulare gleichen Schlüssels übernehmen (fehlt eines, ist sein Anlass vorbei). */
export function restoreUnsaved(container: ParentNode, unsaved: UnsavedInput): void {
  for (const [key, fields] of unsaved) {
    const form = Array.from(container.querySelectorAll<HTMLFormElement>('form[data-cockpit-form]')).find(
      (candidate) => candidate.dataset.cockpitForm === key,
    )
    if (!form) continue
    const elements = Array.from(form.elements)
    for (const field of fields) {
      const named = elements.filter((element) => (element as HTMLInputElement).name === field.name)
      if (field.check) {
        const input = named.find(
          (element): element is HTMLInputElement =>
            element instanceof HTMLInputElement && element.value === field.value,
        )
        if (input) input.checked = field.checked
        continue
      }
      const element = named[0]
      if (element instanceof HTMLSelectElement) {
        // Nur Werte, die es noch gibt (die Auswahl der Personen kann sich geändert haben)
        if (Array.from(element.options).some((option) => option.value === field.value)) element.value = field.value
      } else if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        element.value = field.value
      }
    }
    form.dataset.cockpitGeaendert = '1'
  }
}

interface SwapDetail {
  target?: Element
  elt?: Element
  requestConfig?: { elt?: Element }
}

export const meetingCockpit = defineComponent(() => {
  let socket: WebSocket | null = null
  let timer: number | null = null
  let reconnectTimer: number | null = null
  let queued: number | null = null
  let attempts = 0
  let pending = false
  let stopped = false
  let wsPath = ''
  let unsaved: UnsavedInput | null = null
  const listeners: Array<[EventTarget, string, EventListener]> = []

  return {
    live: 'connecting' as LiveMode,

    init() {
      wsPath = this.$el.dataset.wsPath ?? ''
      const listen = (target: EventTarget, type: string, handler: EventListener) => {
        target.addEventListener(type, handler)
        listeners.push([target, type, handler])
      }
      const stand = () => document.getElementById(STATE_ID)
      // Nach dem Verlassen eines Feldes die aufgeschobene Aktualisierung nachholen (nach dem Fokuswechsel)
      listen(document, 'focusout', () => {
        if (pending) window.setTimeout(() => this.refresh(), 0)
      })
      const onInput: EventListener = (event) => markUnsaved(event.target, stand())
      listen(this.$el, 'input', onInput)
      listen(this.$el, 'change', onInput)
      // Vor dem Austausch des Stands die Eingaben sichern, danach in die neuen Formulare übernehmen
      listen(this.$el, 'htmx:beforeSwap', (event) => {
        const detail = (event as CustomEvent<SwapDetail>).detail
        if (detail?.target?.id !== STATE_ID) return
        const sender = detail.requestConfig?.elt ?? detail.elt
        const sent = sender?.closest<HTMLFormElement>('form[data-cockpit-form]')?.dataset.cockpitForm ?? null
        unsaved = collectUnsaved(detail.target, sent)
      })
      listen(this.$el, 'htmx:afterSwap', (event) => {
        const swapped = event.target
        if (!(swapped instanceof Element) || swapped.id !== STATE_ID || !unsaved) return
        restoreUnsaved(swapped, unsaved)
        unsaved = null
      })
      listen(this.$el, RELOAD_EVENT, () => this.refresh())
      this.schedule(POLL_MS)
      this.connect()
    },

    destroy() {
      stopped = true
      for (const [target, type, handler] of listeners) target.removeEventListener(type, handler)
      listeners.length = 0
      if (timer !== null) window.clearInterval(timer)
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (queued !== null) window.clearTimeout(queued)
      if (socket) {
        socket.onclose = null
        socket.close()
      }
    },

    connect() {
      if (stopped || !wsPath || typeof WebSocket === 'undefined') {
        this.live = 'poll'
        return
      }
      let ws: WebSocket
      try {
        ws = new WebSocket(socketUrl(wsPath, window.location))
      } catch {
        this.live = 'poll'
        return
      }
      socket = ws
      ws.onmessage = (event: MessageEvent) => {
        let message: { type?: string } = {}
        try {
          message = JSON.parse(String(event.data)) as { type?: string }
        } catch {
          return
        }
        if (message.type === 'connected') {
          attempts = 0
          this.live = 'ws'
          this.schedule(SAFETY_POLL_MS)
          // Was zwischen Seitenaufbau und Verbindung geschah, sofort nachholen
          this.refresh()
        } else if (message.type === 'stand') {
          this.queueRefresh()
        }
      }
      ws.onclose = () => {
        if (socket === ws) socket = null
        if (stopped) return
        this.live = 'poll'
        this.schedule(POLL_MS)
        const delay = RECONNECT_MS[Math.min(attempts, RECONNECT_MS.length - 1)]
        attempts += 1
        reconnectTimer = window.setTimeout(() => this.connect(), delay)
      }
    },

    schedule(interval: number) {
      if (timer !== null) window.clearInterval(timer)
      timer = window.setInterval(() => this.refresh(), interval)
    },

    queueRefresh() {
      if (queued !== null) return
      queued = window.setTimeout(() => {
        queued = null
        this.refresh()
      }, COALESCE_MS)
    },

    refresh() {
      const el = document.getElementById(STATE_ID)
      if (!el) return
      if (isEditing(el, document.activeElement)) {
        pending = true
        return
      }
      pending = false
      window.htmx.trigger(el, 'cockpit:refresh')
    },
  }
})
