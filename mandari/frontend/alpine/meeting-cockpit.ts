/**
 * Sitzungscockpit des Session RIS (Issue #140): Stand live halten.
 *
 * Markup: `templates/session/cockpit/page.html` (`x-data="meetingCockpit"`, `data-ws-path`). Der Stand
 * `#cockpit-stand` ist ein HTMX-Fragment mit `hx-trigger="cockpit:refresh"`; diese Komponente löst das
 * Ereignis aus:
 *
 * - bei jedem Hinweis über den WebSocket (`{"type": "stand"}`) – der Socket trägt bewusst keinen Inhalt,
 *   jede Ansicht holt ihren Stand mit den eigenen Rechten ab;
 * - ohne Verbindung alle zwei Sekunden (Polling-Rückfall), mit Verbindung zur Sicherheit alle 30 Sekunden.
 *
 * Der Server antwortet mit 204, solange sich nichts geändert hat (`?v=` im Fragment). Während jemand im Stand
 * tippt (Stimmenzahlen, Vermerk), wartet die Aktualisierung, damit keine Eingabe verloren geht.
 */

import { defineComponent } from '../js/alpine/component'

export const POLL_MS = 2000
export const SAFETY_POLL_MS = 30000
const RECONNECT_MS = [1000, 2000, 5000, 10000, 30000]
const STATE_ID = 'cockpit-stand'

export type LiveMode = 'connecting' | 'ws' | 'poll'

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

export const meetingCockpit = defineComponent(() => {
  let socket: WebSocket | null = null
  let timer: number | null = null
  let reconnectTimer: number | null = null
  let attempts = 0
  let pending = false
  let stopped = false
  let wsPath = ''
  let onFocusOut: (() => void) | null = null

  return {
    live: 'connecting' as LiveMode,

    init() {
      wsPath = this.$el.dataset.wsPath ?? ''
      // Nach dem Verlassen eines Feldes die aufgeschobene Aktualisierung nachholen (nach dem Fokuswechsel)
      onFocusOut = () => {
        if (pending) window.setTimeout(() => this.refresh(), 0)
      }
      document.addEventListener('focusout', onFocusOut)
      this.schedule(POLL_MS)
      this.connect()
    },

    destroy() {
      stopped = true
      if (onFocusOut) document.removeEventListener('focusout', onFocusOut)
      if (timer !== null) window.clearInterval(timer)
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
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
          this.refresh()
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
