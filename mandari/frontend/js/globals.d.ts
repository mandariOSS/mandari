import type { Alpine as AlpineType } from 'alpinejs'
import type htmx from 'htmx.org'
import type { ConfirmOptions } from './alpine/confirm-dialog'

declare global {
  interface Window {
    htmx: typeof htmx
    Alpine: AlpineType
    lucide: { createIcons: () => void }
    showToast: (message: string, type?: string) => void
    confirmAction: (options: ConfirmOptions) => Promise<boolean>
  }
}
