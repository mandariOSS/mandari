/**
 * Neuer Rahmen von Work (Issue #852, templates/work/base_work_neu.html): Dunkelmodus, schmale Seitenleiste,
 * Raum-Dialog, Blatt „Mehr“ am Handy, Suche mit Strg+K, Glocke und Personenmenü. Vorher Inline-Objekte im Layout
 * (templates/work/base_work_alt.html); die Engineering Standards verlangen Alpine.data() in frontend/.
 *
 * Markup: `<html x-data="workRahmen" data-rahmen="neu">`. Dialoge und Blätter sind modal: Escape schließt sie, der
 * Fokus kehrt auf den auslösenden Knopf zurück (Fokusfalle per x-trap im Markup).
 */

import { defineComponent } from '../js/alpine/component'
import { csrfToken } from '../js/csrf'

/** Nach dieser Zeit ohne Ladeereignis zeigt die Dokumentansicht einen Fehler statt eines Ladekreises. */
export const DOC_TIMEOUT_MS = 30_000
/** Abstand der Abfrage ungelesener Benachrichtigungen (wie im bisherigen Rahmen). */
export const GLOCKE_INTERVALL_MS = 30_000

function lesen(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function schreiben(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Speicher gesperrt (privater Modus): die Einstellung wirkt dann nur auf dieser Seite
  }
}

/** Ctrl+K bzw. ⌘K: öffnet die Suche, auch wenn gerade ein Feld den Fokus hat. */
export function istSuchKuerzel(event: KeyboardEvent): boolean {
  return (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k'
}

function fokusZurueck(element: HTMLElement | null): void {
  if (element && document.contains(element)) element.focus()
}

export const workRahmen = defineComponent(() => ({
  darkMode: lesen('darkMode') === 'true',
  /** Seitenleiste eingeklappt (gleicher Schlüssel wie im bisherigen Rahmen, die Wahl bleibt erhalten) */
  leisteSchmal: lesen('sidebarCollapsed') === 'true',
  mehrOffen: false,
  raumOffen: false,
  _raumAusloeser: null as HTMLElement | null,
  docViewerUrl: '',
  docViewerName: '',
  docViewerMeta: '',
  docViewerLoading: false,
  docViewerError: false,
  _docTimeout: undefined as ReturnType<typeof setTimeout> | undefined,

  init() {
    const html = this.$el as HTMLElement
    html.dataset.leiste = this.leisteSchmal ? 'schmal' : 'breit'
    this.$watch('darkMode', (value: boolean) => schreiben('darkMode', String(value)))
    this.$watch('leisteSchmal', (value: boolean) => {
      html.dataset.leiste = value ? 'schmal' : 'breit'
      schreiben('sidebarCollapsed', String(value))
    })
    window.addEventListener('keydown', (event: KeyboardEvent) => {
      if (!istSuchKuerzel(event)) return
      event.preventDefault()
      this.sucheOeffnen()
    })
  },

  leisteUmschalten() {
    this.leisteSchmal = !this.leisteSchmal
  },

  /** Suchfeld der Kopfzeile fokussieren; am Handy (Feld verborgen) zur Suchseite wechseln. */
  sucheOeffnen() {
    const feld = document.getElementById('kopf-suche') as HTMLInputElement | null
    if (feld && feld.offsetParent !== null) {
      feld.focus()
      feld.select()
      return
    }
    const ziel = feld?.form?.action
    if (ziel) window.location.assign(ziel)
  },

  openMehr() {
    this.mehrOffen = true
  },

  /** Schließt das Blatt „Mehr“ und gibt den Fokus an den Knopf in der Leiste zurück. */
  closeMehr() {
    if (!this.mehrOffen) return
    this.mehrOffen = false
    this.$nextTick(() => fokusZurueck((this.$refs.mehrButton as HTMLElement | undefined) ?? null))
  },

  /** Raum wechseln; aus dem Blatt „Mehr“ heraus erst das Blatt schließen, sonst liegen zwei Dialoge übereinander. */
  raumOeffnen(ausloeser?: HTMLElement) {
    this._raumAusloeser = ausloeser ?? (document.activeElement as HTMLElement | null)
    if (this.mehrOffen) {
      this.mehrOffen = false
      this._raumAusloeser = (this.$refs.mehrButton as HTMLElement | undefined) ?? this._raumAusloeser
    }
    this.raumOffen = true
  },

  raumSchliessen() {
    if (!this.raumOffen) return
    this.raumOffen = false
    const ausloeser = this._raumAusloeser
    this.$nextTick(() => fokusZurueck(ausloeser))
  },

  openDoc(url: string, name?: string, meta?: string) {
    this.docViewerUrl = url
    this.docViewerName = name || 'Dokument'
    this.docViewerMeta = meta || ''
    this.docViewerLoading = true
    this.docViewerError = false
    clearTimeout(this._docTimeout)
    this._docTimeout = setTimeout(() => {
      if (this.docViewerLoading) {
        this.docViewerLoading = false
        this.docViewerError = true
      }
    }, DOC_TIMEOUT_MS)
  },
}))

/**
 * Glocke in der Kopfzeile: Zahl ungelesener Benachrichtigungen (alle 30 s), Liste beim Öffnen, „Alle gelesen“.
 * Adressen aus data-zahl-url, data-liste-url, data-gelesen-url am Element.
 */
export const workGlocke = defineComponent(() => ({
  offen: false,
  anzahl: 0,
  laedt: false,
  geladen: false,
  _urls: { zahl: '', liste: '', gelesen: '' },

  init() {
    const data = (this.$el as HTMLElement).dataset
    this._urls = { zahl: data.zahlUrl ?? '', liste: data.listeUrl ?? '', gelesen: data.gelesenUrl ?? '' }
    void this.zahlLaden()
    setInterval(() => void this.zahlLaden(), GLOCKE_INTERVALL_MS)
    window.addEventListener('notification:marked-read', () => void this.listeLaden())
  },

  get beschriftung(): string {
    if (this.anzahl <= 0) return 'Benachrichtigungen'
    return this.anzahl === 1 ? 'Benachrichtigungen, 1 ungelesen' : `Benachrichtigungen, ${this.anzahl} ungelesen`
  },

  umschalten() {
    if (this.offen) {
      this.schliessen(false)
      return
    }
    this.offen = true
    void this.listeLaden()
  },

  schliessen(fokus = true) {
    if (!this.offen) return
    this.offen = false
    if (fokus) this.$nextTick(() => fokusZurueck((this.$refs.knopf as HTMLElement | undefined) ?? null))
  },

  async zahlLaden() {
    if (!this._urls.zahl) return
    try {
      const antwort = await fetch(this._urls.zahl, { headers: { Accept: 'application/json' } })
      if (!antwort.ok) return
      const daten = (await antwort.json()) as { count?: number }
      this.anzahl = Number(daten.count ?? 0)
    } catch (error) {
      console.warn('Benachrichtigungen: Zahl nicht geladen', error)
    }
  },

  async listeLaden() {
    if (this.laedt || !this._urls.liste) return
    this.laedt = true
    try {
      const antwort = await fetch(this._urls.liste, { headers: { Accept: 'application/json' } })
      if (antwort.ok) {
        const daten = (await antwort.json()) as { html?: string; unread_count?: number }
        // Fertiges Markup des Servers (templates/work/notifications/partials/…), wie im bisherigen Rahmen
        ;(this.$refs.liste as HTMLElement).innerHTML = daten.html ?? ''
        this.anzahl = Number(daten.unread_count ?? 0)
        this.geladen = true
      }
    } catch (error) {
      console.warn('Benachrichtigungen: Liste nicht geladen', error)
    }
    this.laedt = false
  },

  async alleGelesen() {
    if (!this._urls.gelesen) return
    try {
      const antwort = await fetch(this._urls.gelesen, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() } })
      if (antwort.ok) {
        this.anzahl = 0
        await this.listeLaden()
      }
    } catch (error) {
      console.warn('Benachrichtigungen: nicht als gelesen markiert', error)
    }
  },
}))

interface InstallPrompt {
  prompt: () => void
  userChoice: Promise<{ outcome?: string } | undefined>
}

/** Personenmenü unten in der Leiste: Profil, Sicherheit, Benachrichtigungen, App installieren, Abmelden. */
export const workPerson = defineComponent(() => ({
  offen: false,
  canInstall: !!(window as unknown as { mandariInstallPrompt?: InstallPrompt }).mandariInstallPrompt,
  installHidden: lesen('pwaInstallHidden') === '1',

  umschalten() {
    this.offen = !this.offen
  },

  schliessen(fokus = true) {
    if (!this.offen) return
    this.offen = false
    if (fokus) this.$nextTick(() => fokusZurueck((this.$refs.knopf as HTMLElement | undefined) ?? null))
  },

  async installApp() {
    const fenster = window as unknown as { mandariInstallPrompt?: InstallPrompt | null }
    const prompt = fenster.mandariInstallPrompt
    if (!prompt) return
    prompt.prompt()
    const choice = await prompt.userChoice
    fenster.mandariInstallPrompt = null
    this.canInstall = false
    if (choice && choice.outcome === 'dismissed') this.hideInstall()
  },

  hideInstall() {
    this.installHidden = true
    schreiben('pwaInstallHidden', '1')
  },
}))
