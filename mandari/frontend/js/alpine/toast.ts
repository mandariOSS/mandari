/**
 * Toast-Benachrichtigungen.
 *
 * Markup liegt im Layout (`x-data="toastManager"`), Django-Messages kommen als JSON aus
 * `<script type="application/json" id="django-messages">`. `showToast()` ist global.
 *
 * Eine Anzeige für alles: Rückmeldungen des Servers, Fehler von Anfragen und die Hinweise auf neue Benachrichtigungen
 * (`static/js/notifications.js`, Ereignis `show-toast` mit `titel` und `link`). Jede Meldung steht einmal da:
 * dieselbe Meldung noch einmal verlängert nur ihre Frist, und eine aus dem Verlauf zurückgeholte Seite zeigt ihre
 * Meldungen nicht erneut.
 */

import { defineComponent } from './component'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface Toast {
  id: number
  message: string
  type: ToastType
  visible: boolean
  /** Überschrift über dem Text, etwa der Titel einer Benachrichtigung; sonst leer */
  titel: string
  /** Ziel beim Klick auf den Text (nur Adressen dieser Anwendung); sonst leer */
  link: string
}

/** Zusätze im Ereignis `show-toast` */
export interface ToastZusatz {
  titel?: unknown
  link?: unknown
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
/** Steht der Zeiger auf den Meldungen oder liegt der Tastaturfokus darin, wird nach dieser Zeit neu geprüft */
const NACHFRIST_MS = 1500
/** Mehr Meldungen stehen nie gleichzeitig; die älteste weicht */
export const MAX_SICHTBAR = 4
/** Im Browser gemerkte Seitenstände, deren Meldungen schon gezeigt wurden */
const GEZEIGT_SCHLUESSEL = 'mandari:meldungen-gezeigt'
const GEZEIGT_HOECHSTENS = 20
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
 * Ob die Meldungen dieses Seitenstands schon gezeigt wurden; merkt ihn sich sonst. Holt der Browser eine Seite aus dem
 * Verlauf zurück („Zurück“), zeigt er die gespeicherte Antwort samt ihrer Meldungen noch einmal: „Gespeichert“ erschien
 * so erneut, obwohl nichts gespeichert wurde. Ohne Kennung oder ohne Speicher gilt die Meldung als neu.
 */
export function schonGezeigt(marke: string, speicher: Pick<Storage, 'getItem' | 'setItem'> | null): boolean {
  if (!marke || !speicher) return false
  try {
    const gemerkt: unknown = JSON.parse(speicher.getItem(GEZEIGT_SCHLUESSEL) ?? '[]')
    const liste = Array.isArray(gemerkt)
      ? gemerkt.filter((eintrag): eintrag is string => typeof eintrag === 'string')
      : []
    if (liste.includes(marke)) return true
    speicher.setItem(GEZEIGT_SCHLUESSEL, JSON.stringify([...liste, marke].slice(-GEZEIGT_HOECHSTENS)))
  } catch {
    // Speicher gesperrt oder voll: lieber einmal zu viel zeigen als gar nicht
  }
  return false
}

function sitzungsspeicher(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

/** Adresse innerhalb dieser Anwendung als Pfad, sonst leer (kein Sprung zu fremden Seiten über eine Meldung) */
export function sichererLink(link: unknown, herkunft: string): string {
  if (typeof link !== 'string' || !link) return ''
  try {
    const ziel = new URL(link, herkunft)
    return ziel.origin === herkunft ? `${ziel.pathname}${ziel.search}${ziel.hash}` : ''
  } catch {
    return ''
  }
}

/** Dieselbe Meldung, die gerade schon dasteht: gleicher Text, gleiche Art, gleiche Überschrift, gleiches Ziel */
export function gleicheMeldung(
  toasts: Toast[],
  message: string,
  type: ToastType,
  titel: string,
  link: string,
): Toast | undefined {
  return toasts.find(
    (t) => t.visible && t.message === message && t.type === type && t.titel === titel && t.link === link,
  )
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
export const toastManager = defineComponent(() => {
  /** Laufende Fristen je Meldung; außerhalb des beobachteten Zustands, weil die Anzeige nicht davon abhängt */
  const fristen = new Map<number, number>()

  return {
    toasts: [] as Toast[],
    nextId: 1,

    init() {
      const meldungen = djangoMessages()
      // Kennung des Seitenstands am Container (`data-marke`), nur wenn die Seite Meldungen trägt
      const marke = (this.$root as HTMLElement).dataset.marke ?? ''
      if (meldungen.length && !schonGezeigt(marke, sitzungsspeicher())) {
        for (const item of meldungen) this.addToast(item.message, item.tags)
      }
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

    addToast(message: string, type = 'info', zusatz: ToastZusatz | null = null) {
      this.ueberLeistenLegen()
      const art = normalizeType(type)
      const titel = typeof zusatz?.titel === 'string' ? zusatz.titel : ''
      // Dieselbe Meldung noch einmal (etwa der Fehler jedes regelmäßigen Abrufs): bleibt eine, nur länger
      const link = sichererLink(zusatz?.link, window.location.origin)
      const vorhanden = gleicheMeldung(this.toasts, message, art, titel, link)
      if (vorhanden) {
        this.fristSetzen(vorhanden.id)
        return
      }
      const id = this.nextId++
      this.toasts.push({ id, message, type: art, visible: true, titel, link })
      const sichtbar = this.toasts.filter((t) => t.visible)
      for (const aelteste of sichtbar.slice(0, Math.max(0, sichtbar.length - MAX_SICHTBAR)))
        this.removeToast(aelteste.id)
      this.fristSetzen(id)
    },

    /** Die Meldung bleibt stehen, solange jemand damit umgeht (Zeiger darauf, Fokus darin) */
    inBenutzung(): boolean {
      const wurzel = this.$root as HTMLElement
      // Am Touch-Gerät bliebe „hover“ nach einem Tipp bis zum nächsten Tipp bestehen
      const zeiger = window.matchMedia('(hover: hover)').matches && wurzel.matches(':hover')
      return zeiger || wurzel.matches(':focus-within')
    },

    fristSetzen(id: number, dauer = DURATION_MS) {
      window.clearTimeout(fristen.get(id))
      fristen.set(
        id,
        window.setTimeout(() => {
          if (this.inBenutzung()) this.fristSetzen(id, NACHFRIST_MS)
          else this.removeToast(id)
        }, dauer),
      )
    },

    removeToast(id: number) {
      window.clearTimeout(fristen.get(id))
      fristen.delete(id)
      const toast = this.toasts.find((t) => t.id === id)
      if (!toast) return
      toast.visible = false
      window.setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id)
      }, LEAVE_MS)
    },
  }
})
