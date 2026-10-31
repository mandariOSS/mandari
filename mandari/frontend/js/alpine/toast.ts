/**
 * Toast-Benachrichtigungen.
 *
 * Markup liegt im Layout (`x-data="toastManager"`), Django-Messages kommen als JSON aus
 * `<script type="application/json" id="django-messages">`. `showToast()` ist global.
 */

import { defineComponent } from './component'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface Toast {
  id: number
  message: string
  type: ToastType
  visible: boolean
}

const TYPE_MAP: Record<string, ToastType> = {
  success: 'success',
  error: 'error',
  danger: 'error',
  warning: 'warning',
  info: 'info',
  debug: 'info',
}

const DURATION_MS = 5000
const LEAVE_MS = 300
/** Eine feste Leiste endet höchstens so weit über dem Fensterrand; darunter liegt am Handy nur die Leiste „Hauptbereiche“ */
const BEREICH_UNTEN_PX = 120
const ABSTAND_ZUR_LEISTE_PX = 12

export function normalizeType(type: string | undefined): ToastType {
  if (!type) return 'info'
  for (const tag of type.split(/\s+/)) {
    const mapped = TYPE_MAP[tag]
    if (mapped) return mapped
  }
  return 'info'
}

export function showToast(message: string, type = 'info'): void {
  window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, type: normalizeType(type) } }))
}

function djangoMessages(): Array<{ message: string; tags?: string }> {
  const el = document.getElementById('django-messages')
  if (!el?.textContent) return []
  try {
    const parsed: unknown = JSON.parse(el.textContent)
    return Array.isArray(parsed) ? (parsed as Array<{ message: string; tags?: string }>) : []
  } catch {
    return []
  }
}

/**
 * Abstand der Meldungen vom unteren Fensterrand, wenn dort eine feste Leiste steht (`data-meldungen-darueber`), sonst
 * `null`. Meldungen nehmen Klicks an: Auf der Leiste blockierten sie für 5 s deren Knöpfe, etwa „Nächster TOP“ (#1018).
 * Gemessen statt fest eingetragen, weil die Leisten ihre Höhe ändern (Umbruch am Handy, aufgeklappter Beschluss).
 */
export function abstandUeberLeisten(leisten: Iterable<Element>, fensterhoehe: number): number | null {
  let oben = Number.POSITIVE_INFINITY
  for (const leiste of leisten) {
    const { top, bottom, height } = leiste.getBoundingClientRect()
    if (height > 0 && top < fensterhoehe && bottom >= fensterhoehe - BEREICH_UNTEN_PX) oben = Math.min(oben, top)
  }
  return Number.isFinite(oben) ? fensterhoehe - oben + ABSTAND_ZUR_LEISTE_PX : null
}

/** Alpine-Komponente: `x-data="toastManager"` */
export const toastManager = defineComponent(() => ({
  toasts: [] as Toast[],
  nextId: 1,

  init() {
    for (const item of djangoMessages()) this.addToast(item.message, item.tags)
    // Eine Leiste kann per HTMX wechseln oder im schmaleren Fenster umbrechen, während eine Meldung steht
    const nachfuehren = () => {
      if (this.toasts.length) this.ueberLeistenLegen()
    }
    document.body.addEventListener('htmx:afterSettle', nachfuehren)
    window.addEventListener('resize', nachfuehren)
  },

  ueberLeistenLegen() {
    const abstand = abstandUeberLeisten(document.querySelectorAll('[data-meldungen-darueber]'), window.innerHeight)
    ;(this.$root as HTMLElement).style.bottom = abstand === null ? '' : `${abstand}px`
  },

  addToast(message: string, type = 'info') {
    this.ueberLeistenLegen()
    const id = this.nextId++
    this.toasts.push({ id, message, type: normalizeType(type), visible: true })
    window.setTimeout(() => this.removeToast(id), DURATION_MS)
  },

  removeToast(id: number) {
    const toast = this.toasts.find((t) => t.id === id)
    if (!toast) return
    toast.visible = false
    window.setTimeout(() => {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    }, LEAVE_MS)
  },
}))
