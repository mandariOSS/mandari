/**
 * Toast-Benachrichtigungen.
 *
 * Markup liegt im Layout (`x-data="toastManager"`), Django-Messages kommen als JSON aus
 * `<script type="application/json" id="django-messages">`. `showToast()` ist global.
 */

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

/** Alpine-Komponente: `x-data="toastManager"` */
export function toastManager() {
  return {
    toasts: [] as Toast[],
    nextId: 1,

    init() {
      for (const item of djangoMessages()) this.addToast(item.message, item.tags)
    },

    addToast(message: string, type = 'info') {
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
  }
}
