/**
 * Bestätigungsdialog.
 *
 * Markup liegt im Layout (`x-data="confirmDialog"`); `confirmAction()` ist global und
 * liefert ein Promise<boolean>. Wird auch für `hx-confirm` verwendet.
 */

export type ConfirmVariant = 'danger' | 'warning' | 'info'

export interface ConfirmOptions {
  title?: string
  message?: string
  confirmText?: string
  variant?: ConfirmVariant
}

interface ConfirmEventDetail extends ConfirmOptions {
  _cb?: (ok: boolean) => void
}

export function confirmAction(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    const detail: ConfirmEventDetail = { ...options, _cb: resolve }
    window.dispatchEvent(new CustomEvent('confirm-dialog', { detail }))
  })
}

/** Alpine-Komponente: `x-data="confirmDialog"` */
export function confirmDialog() {
  return {
    open: false,
    title: '',
    message: '',
    confirmText: 'Bestätigen',
    variant: 'danger' as ConfirmVariant,
    _resolve: null as ((ok: boolean) => void) | null,
    _previousFocus: null as HTMLElement | null,

    init() {
      window.addEventListener('confirm-dialog', (event) => {
        const detail = (event as CustomEvent<ConfirmEventDetail>).detail ?? {}
        void this.show(detail).then((ok) => detail._cb?.(ok))
      })
    },

    show(opts: ConfirmOptions): Promise<boolean> {
      this._previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
      this.title = opts.title || 'Bestätigen'
      this.message = opts.message || ''
      this.confirmText = opts.confirmText || 'Bestätigen'
      this.variant = opts.variant || 'danger'
      this.open = true
      this.$nextTick(() => {
        this.$refs.confirmBtn?.focus()
      })
      return new Promise((resolve) => {
        this._resolve = resolve
      })
    },

    _finish(ok: boolean) {
      this.open = false
      this._resolve?.(ok)
      this._resolve = null
      this._previousFocus?.focus()
      this._previousFocus = null
    },

    accept() {
      this._finish(true)
    },

    dismiss() {
      if (this.open) this._finish(false)
    },

    // Alpine-Magics, damit TypeScript sie kennt
    $nextTick: (() => undefined) as (cb: () => void) => void,
    $refs: {} as Record<string, HTMLElement | undefined>,
  }
}
