/**
 * mandari – zentraler Frontend-Einstieg (Vite, django-vite).
 *
 * Reihenfolge: HTMX konfigurieren, Alpine-Komponenten und -Stores registrieren,
 * Icon-Observer starten, Alpine starten. Die Globals `htmx`, `Alpine`, `lucide`,
 * `showToast`, `confirmAction`, `bereinigeKiHtml` und `kiMarkdownHtml` bleiben für Templates erhalten.
 */

import collapse from '@alpinejs/collapse'
import focus from '@alpinejs/focus'
import Alpine from 'alpinejs'
import { documentText, merklisteController, neighborhoodSubscription, questionForm } from '../alpine/insight'
import { insightShell } from '../alpine/insight-shell'
import { kommunenWahl } from '../alpine/kommunen-wahl'
import { meetingCockpit } from '../alpine/meeting-cockpit'
import { neighborhoodApp } from '../alpine/neighborhood'
import { workGruppe, workPerson, workRahmen } from '../alpine/work-rahmen'
import { installActions } from './actions'
import { confirmAction, confirmDialog } from './alpine/confirm-dialog'
import { showToast, toastManager } from './alpine/toast'
import { installFormBehaviors } from './form-behaviors'
import { setupHtmx } from './htmx-setup'
import { installIconObserver, renderIcons } from './icons'
import { bereinigeKiHtml, kiMarkdownHtml } from './ki-ausgabe'
import { initMeetingMap } from './meeting-map'
import { initPaperMap } from './paper-map'
import { initSearchPlaceMap } from './search-place-map'
import { registerBookmarksStore } from './stores/bookmarks'
import { installTagesordnungSortieren } from './tagesordnung-sortieren'

// ---- HTMX --------------------------------------------------------------------
setupHtmx()

// ---- Deklarative Aktionen (data-confirm, data-href, … statt Inline-Handlern, #172) ----
installActions()
// Formularhelfer (URL-Kürzel, abhängige Auswahl, Textbausteine, Fehlerbericht) statt Inline-Skripten
installFormBehaviors()

// ---- Globals für Templates ----------------------------------------------------
window.Alpine = Alpine
window.lucide = { createIcons: () => renderIcons() }
window.showToast = showToast
window.confirmAction = confirmAction
// KI-Antworten nur bereinigt als HTML ausgeben (KI-Assistent, pages/chat.html)
window.bereinigeKiHtml = bereinigeKiHtml
window.kiMarkdownHtml = kiMarkdownHtml

// ---- Alpine: Plugins, Komponenten-Registry, Stores ---------------------------
Alpine.plugin(collapse)
Alpine.plugin(focus)

Alpine.data('toastManager', toastManager)
Alpine.data('confirmDialog', confirmDialog)
// Session RIS: Sitzungscockpit (Issue #140) – das Session-Layout lädt nur dieses Bundle
Alpine.data('meetingCockpit', meetingCockpit)

if (document.documentElement.dataset.portal === 'insight') {
  // Rahmen (Menü, Kommunenwechsel, Dokumentansicht; Issue #783) und Seitenkomponenten (vorher Inline-Skripte, #172)
  Alpine.data('insightShell', insightShell)
  Alpine.data('kommunenWahl', kommunenWahl)
  Alpine.data('merklisteController', merklisteController)
  Alpine.data('questionForm', questionForm)
  Alpine.data('neighborhoodSubscription', neighborhoodSubscription)
  Alpine.data('documentText', documentText)
  Alpine.data('neighborhoodApp', neighborhoodApp)
  registerBookmarksStore(Alpine)
  // Vorgangsseite: Orte und amtliche Umringe; Sitzungsseite: Sitzungsort (Leaflet als Vendor-Skript, läuft vor diesem Modul)
  initPaperMap()
  initMeetingMap()
  // Suche: Karte im Ortsband (auch nach HTMX-Austausch, siehe search-place-map.ts)
  try {
    initSearchPlaceMap()
  } catch (error) {
    console.warn('Karte im Ortsband nicht gezeichnet', error)
  }
}

if (document.documentElement.dataset.portal === 'session') {
  // Neuer Rahmen des Sitzungsdienstes (Issue #944): dieselben Komponenten wie der Rahmen von Work (Leiste, Mandant
  // wechseln, Blatt „Mehr“, Personenmenü); Tagesordnung per Ziehen umsortieren ohne Inline-Skript
  Alpine.data('workRahmen', workRahmen)
  Alpine.data('workGruppe', workGruppe)
  Alpine.data('workPerson', workPerson)
  installTagesordnungSortieren()
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
// nachträglich eingefügt), sofort starten. Gestartet wird in einer eigenen Aufgabe:
// Sonst liefen Auswertung der Module, HTMX und der Aufbau aller Komponenten als eine
// lange Aufgabe, die den Hauptthread blockiert (Total Blocking Time).
const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
const domContentLoadedStarted = document.readyState === 'complete' || (navigation?.domContentLoadedEventStart ?? 0) > 0
if (domContentLoadedStarted) {
  Alpine.start()
} else {
  document.addEventListener('DOMContentLoaded', () => setTimeout(() => Alpine.start()), { once: true })
}
