/**
 * Sitzungskalender in Work (templates/work/meetings/neu/kalender.html, Issue #951), vorher Inline-Skript mit Inline-CSS
 * in templates/work/meetings/calendar.html.
 *
 * Markup: `<div x-data="sitzungsKalender" data-ereignisse="…/meetings/calendar/events/">` mit der Fläche
 * `[data-kalender]`. FullCalendar kommt wie bisher lokal aus static/vendor/fullcalendar (globales `FullCalendar`,
 * vor diesem Modul geladen). Einstellungen wie bisher: Monat, Woche und Liste, am Handy die Liste; ein Klick öffnet
 * die Sitzung, der volle Titel steht als Tooltip am Termin.
 *
 * Farben, Rahmen und der Dunkelmodus stehen nicht mehr hier, sondern als Tokens in static/css/input.css
 * (`.sitzungs-kalender`, Variablen `--fc-*`). Damit entfallen die feste Farbe und der Beobachter des Dunkelmodus.
 */

import { defineComponent } from '../js/alpine/component'

interface KalenderTermin {
  url: string
  extendedProps: { fullTitle?: string }
}

interface KalenderKlick {
  event: KalenderTermin
  jsEvent: Event
}

interface KalenderEintrag {
  event: KalenderTermin
  el: HTMLElement
}

interface Kalender {
  render(): void
  destroy(): void
}

interface FullCalendarGlobal {
  Calendar: new (element: HTMLElement, options: Record<string, unknown>) => Kalender
}

/** Unter 640 px: Liste statt Monatsraster und eine kurze Werkzeugleiste (wie bisher) */
const SCHMAL = '(max-width: 639px)'

/** Einstellungen des Kalenders; `schmal` für Handys */
export function kalenderOptionen(ereignisse: string, schmal: boolean): Record<string, unknown> {
  return {
    initialView: schmal ? 'listMonth' : 'dayGridMonth',
    locale: 'de',
    headerToolbar: schmal
      ? { left: 'prev,next', center: 'title', right: 'listMonth,dayGridMonth' }
      : { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,listMonth' },
    events: ereignisse,
    eventClick(info: KalenderKlick) {
      if (info.event.url) {
        window.location.href = info.event.url
        info.jsEvent.preventDefault()
      }
    },
    eventDidMount(info: KalenderEintrag) {
      // Tooltip mit vollem Titel
      if (info.event.extendedProps.fullTitle) {
        info.el.setAttribute('title', info.event.extendedProps.fullTitle)
      }
    },
    height: 'auto',
    nowIndicator: true,
    dayMaxEvents: 3,
  }
}

export const sitzungsKalender = defineComponent(() => ({
  kalender: null as Kalender | null,

  init() {
    const fc = (window as unknown as { FullCalendar?: FullCalendarGlobal }).FullCalendar
    const flaeche = this.$el.querySelector<HTMLElement>('[data-kalender]')
    const ereignisse = this.$el.dataset.ereignisse ?? ''
    if (!fc || !flaeche || !ereignisse) return
    this.kalender = new fc.Calendar(flaeche, kalenderOptionen(ereignisse, window.matchMedia(SCHMAL).matches))
    this.kalender.render()
  },

  destroy() {
    this.kalender?.destroy()
    this.kalender = null
  },
}))
