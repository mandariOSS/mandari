/**
 * mandari – zentraler Frontend-Einstieg (Vite, django-vite).
 *
 * Reihenfolge: HTMX konfigurieren, Alpine-Komponenten und -Stores registrieren,
 * Icon-Observer starten, Alpine starten. Die Globals `htmx`, `Alpine`, `lucide`,
 * `showToast` und `confirmAction` bleiben für Templates erhalten.
 */

import collapse from '@alpinejs/collapse'
import focus from '@alpinejs/focus'
import Alpine from 'alpinejs'
import { confirmAction, confirmDialog } from './alpine/confirm-dialog'
import { showToast, toastManager } from './alpine/toast'
import { setupHtmx } from './htmx-setup'
import { installIconObserver, renderIcons } from './icons'
import { registerBookmarksStore } from './stores/bookmarks'

// ---- HTMX --------------------------------------------------------------------
setupHtmx()

// ---- Globals für Templates ----------------------------------------------------
window.Alpine = Alpine
window.lucide = { createIcons: () => renderIcons() }
window.showToast = showToast
window.confirmAction = confirmAction

// ---- Alpine: Plugins, Komponenten-Registry, Stores ---------------------------
Alpine.plugin(collapse)
Alpine.plugin(focus)

Alpine.data('toastManager', toastManager)
Alpine.data('confirmDialog', confirmDialog)

if (document.documentElement.dataset.portal === 'insight') {
  registerBookmarksStore(Alpine)
}

// Mobile: Seitenleiste nach Navigation schließen (Layouts halten `sidebarOpen` am <html>)
document.addEventListener('click', (event) => {
  const link = (event.target as Element | null)?.closest('aside a[href]')
  if (!link) return
  const data = Alpine.$data(document.documentElement) as { sidebarOpen?: boolean } | undefined
  if (data && 'sidebarOpen' in data) data.sidebarOpen = false
})

// ---- Icons ---------------------------------------------------------------------
installIconObserver()

// ---- Start ---------------------------------------------------------------------
// Erst nach dem Parsen starten, damit weitere Modul-Einstiege (z. B. der Editor)
// ihre Globals vor der Initialisierung der x-data-Komponenten gesetzt haben.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => Alpine.start(), { once: true })
} else {
  Alpine.start()
}
