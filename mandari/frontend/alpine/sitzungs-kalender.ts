/**
 * Sitzungskalender in Work (templates/work/meetings/neu/kalender.html, Issue #951), vorher Inline-Skript mit Inline-CSS
 * in templates/work/meetings/calendar.html.
 *
 * Markup: `<div x-data="sitzungsKalender" data-ereignisse="…/meetings/calendar/events/" data-arten="sitzung,fraktion">`
 * mit der Fläche `[data-kalender]`. Seit Issue #1489 liefert die Adresse neben den Sitzungen der Gremien auch
 * Fraktionssitzungen, Fristen und Aufgaben; `arten` (Kästchen über dem Kalender) wählt, was erscheint, und der
 * Browser merkt sich die Wahl. FullCalendar kommt wie bisher lokal aus static/vendor/fullcalendar (globales `FullCalendar`,
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
  refetchEvents(): void
}

interface FullCalendarGlobal {
  Calendar: new (element: HTMLElement, options: Record<string, unknown>) => Kalender
}

/** Unter 640 px: Liste statt Monatsraster und eine kurze Werkzeugleiste (wie bisher) */
const SCHMAL = '(max-width: 639px)'

/** Schlüssel im Speicher des Browsers: abgewählte Arten von Terminen */
const ABGEWAEHLT = 'workKalenderAbgewaehlt'

/**
 * Gewählte Arten: alle erlaubten ohne die, die die Person abgewählt hat. Gemerkt wird, was abgewählt ist; eine Art,
 * die später dazukommt, erscheint damit von selbst.
 */
export function gewaehlteArten(erlaubt: string[], abgewaehlt: string | null): string[] {
  const aus = new Set((abgewaehlt ?? '').split(',').filter(Boolean))
  return erlaubt.filter((art) => !aus.has(art))
}

/** Einstellungen des Kalenders; `schmal` für Handys, `arten` liefert die gewählten Arten für jeden Abruf */
export function kalenderOptionen(ereignisse: string, schmal: boolean, arten?: () => string[]): Record<string, unknown> {
  return {
    initialView: schmal ? 'listMonth' : 'dayGridMonth',
    locale: 'de',
    headerToolbar: schmal
      ? { left: 'prev,next', center: 'title', right: 'listMonth,dayGridMonth' }
      : { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,listMonth' },
    events: arten ? { url: ereignisse, extraParams: () => ({ arten: arten().join(',') }) } : ereignisse,
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
  /** Arten, die die Person sehen darf (aus data-arten), und die davon gewählten */
  erlaubt: [] as string[],
  arten: [] as string[],

  init() {
    this.erlaubt = (this.$el.dataset.arten ?? '').split(',').filter(Boolean)
    let gemerkt: string | null = null
    try {
      gemerkt = localStorage.getItem(ABGEWAEHLT)
    } catch {
      // Speicher gesperrt (privater Modus): alle Arten zeigen
    }
    this.arten = gewaehlteArten(this.erlaubt, gemerkt)
    this.$watch('arten', (wahl: string[]) => {
      try {
        localStorage.setItem(ABGEWAEHLT, this.erlaubt.filter((art) => !wahl.includes(art)).join(','))
      } catch {
        // gilt dann nur für diese Seite
      }
      this.kalender?.refetchEvents()
    })

    const fc = (window as unknown as { FullCalendar?: FullCalendarGlobal }).FullCalendar
    const flaeche = this.$el.querySelector<HTMLElement>('[data-kalender]')
    const ereignisse = this.$el.dataset.ereignisse ?? ''
    if (!fc || !flaeche || !ereignisse) return
    const wahl = this.erlaubt.length ? () => [...this.arten] : undefined
    this.kalender = new fc.Calendar(flaeche, kalenderOptionen(ereignisse, window.matchMedia(SCHMAL).matches, wahl))
    this.kalender.render()
  },

  destroy() {
    this.kalender?.destroy()
    this.kalender = null
  },
}))
