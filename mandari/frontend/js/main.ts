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
// Vite-Einstiege sind Module: Sie laufen erst, wenn document.readyState bereits
// "interactive" ist – aber vor DOMContentLoaded und in Dokumentreihenfolge. Startete
// Alpine hier sofort, hätten work.ts und das Editor-Bundle ihre Komponenten noch nicht
// registriert, und jedes x-data dieser Seiten bliebe leer. Deshalb bis
// DOMContentLoaded warten; nur wenn das Ereignis schon ausgelöst wurde (Skript
// nachträglich eingefügt), sofort starten.
const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
const domContentLoadedStarted = document.readyState === 'complete' || (navigation?.domContentLoadedEventStart ?? 0) > 0
if (domContentLoadedStarted) {
  Alpine.start()
} else {
  document.addEventListener('DOMContentLoaded', () => Alpine.start(), { once: true })
}
