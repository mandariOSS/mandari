/**
 * Blattansicht für PDF-Unterlagen der Sitzungsvorbereitung (#856).
 *
 * pdf.js zeichnet die Seiten untereinander in der Breite der Spalte („Seitenbreite“); A−/A+ ändern den
 * Faktor relativ dazu, die Schrift wächst mit. Eine Textebene macht den Text markier- und mit der
 * Browsersuche auffindbar. Gezeichnet wird nur, was sichtbar ist oder gleich sichtbar wird; weit
 * entfernte Seiten geben ihren Canvas wieder frei (lange Haushaltspläne). Die aktuelle Seite (oberes
 * Drittel der Fläche) wird gemeldet – die Anmerkungen zu einer Seite nutzen sie.
 *
 * Kein Alpine-Zustand: Die Instanz hängt außerhalb der Reaktivität an der Komponente `vorbereitung`.
 */

import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import { ladePdfJs, type PdfJsModul } from '../editor/pdfjs-laden'

export type BlattZustand = 'laedt' | 'bereit' | 'fehler'

export interface BlattMeldungen {
  /** Aktuelle Seite und Seitenzahl nach dem Öffnen und beim Blättern */
  seite?: (seite: number, seiten: number) => void
  /** Ladezustand der geöffneten Unterlage */
  zustand?: (zustand: BlattZustand) => void
}

interface Seite {
  nr: number
  el: HTMLDivElement
  /** Größe in PDF-Einheiten bei Skala 1 */
  breite: number
  hoehe: number
  gezeichnet: boolean
  zeichnung: RenderTask | null
}

/** Abstand links und rechts der Seiten (CSS-Pixel) */
const RAND = 24
/** Breiter als das wird eine Seite bei „Seitenbreite“ nicht (2.560-px-Bildschirme) */
const MAX_BREITE = 1240
/** Höchstens so viele Seiten behalten ihren Canvas */
const BEHALTEN = 6

export class PdfBlatt {
  private pdfjs: PdfJsModul | null = null
  private ladeauftrag: PDFDocumentLoadingTask | null = null
  private dokument: PDFDocumentProxy | null = null
  private seiten: Seite[] = []
  private readonly inhalt: HTMLDivElement
  private faktor = 1
  private skala = 1
  private aktuell = 1
  private beobachter: IntersectionObserver | null = null
  private readonly groesse: ResizeObserver | null = null
  private breiteZuletzt = 0
  /** Jede Öffnung bekommt eine Nummer; späte Antworten älterer Öffnungen werden verworfen */
  private lauf = 0
  private scrollPlan = 0
  private resizePlan = 0

  constructor(
    private readonly flaeche: HTMLElement,
    private readonly melden: BlattMeldungen = {},
  ) {
    this.inhalt = document.createElement('div')
    this.inhalt.className = 'vb-seiten'
    flaeche.appendChild(this.inhalt)
    flaeche.addEventListener('scroll', () => this.planeSeitenmeldung(), { passive: true })
    if (typeof ResizeObserver !== 'undefined') {
      this.groesse = new ResizeObserver(() => this.planeNeuaufbau())
      this.groesse.observe(flaeche)
    }
  }

  /** Unterlage öffnen (gleiche Herkunft, Anmeldung per Cookie). Ältere Öffnungen werden abgebrochen. */
  async oeffnen(url: string, faktor: number): Promise<void> {
    const lauf = ++this.lauf
    this.leeren()
    this.faktor = faktor
    this.melden.zustand?.('laedt')
    if (!this.pdfjs) this.pdfjs = await ladePdfJs()
    if (lauf !== this.lauf) return
    if (!this.pdfjs) {
      this.melden.zustand?.('fehler')
      return
    }
    try {
      this.ladeauftrag = this.pdfjs.getDocument({ url })
      const dokument = await this.ladeauftrag.promise
      // Eine neuere Öffnung hat den Ladeauftrag bereits beendet (leeren)
      if (lauf !== this.lauf) return
      this.dokument = dokument
      const erste = await dokument.getPage(1)
      if (lauf !== this.lauf) return
      const basis = erste.getViewport({ scale: 1 })
      this.seiten = Array.from({ length: dokument.numPages }, (_, i) =>
        this.neueSeite(i + 1, basis.width, basis.height),
      )
      this.inhalt.replaceChildren(...this.seiten.map((s) => s.el))
      this.aufbauen()
      this.flaeche.scrollTop = 0
      this.aktuell = 1
      this.melden.seite?.(1, dokument.numPages)
      this.melden.zustand?.('bereit')
    } catch (err) {
      if (lauf !== this.lauf) return
      console.warn('Unterlage konnte nicht geladen werden:', err)
      this.leeren()
      this.melden.zustand?.('fehler')
    }
  }

  /** Zoomfaktor relativ zur Seitenbreite; die aktuelle Stelle bleibt im Blick. */
  zoom(faktor: number): void {
    if (faktor === this.faktor) return
    this.faktor = faktor
    if (this.seiten.length) this.aufbauen(true)
  }

  /** Zur Seite springen (1-basiert). */
  zurSeite(nr: number): void {
    const seite = this.seiten[Math.min(this.seiten.length, Math.max(1, nr)) - 1]
    if (!seite) return
    const oben = seite.el.getBoundingClientRect().top - this.flaeche.getBoundingClientRect().top
    this.flaeche.scrollTop += oben - 8
  }

  /** Geöffnete Unterlage schließen (z. B. Wechsel zu einem Link oder einem TOP ohne Unterlagen). */
  schliessen(): void {
    this.lauf++
    this.leeren()
  }

  /** Beobachter lösen (Komponente wird abgebaut). */
  beenden(): void {
    this.schliessen()
    this.groesse?.disconnect()
  }

  // ---- intern ---------------------------------------------------------------------

  private neueSeite(nr: number, breite: number, hoehe: number): Seite {
    const el = document.createElement('div')
    el.className = 'vb-seite'
    el.dataset.seite = String(nr)
    return { nr, el, breite, hoehe, gezeichnet: false, zeichnung: null }
  }

  private leeren(): void {
    window.clearTimeout(this.resizePlan)
    this.beobachter?.disconnect()
    this.beobachter = null
    for (const s of this.seiten) s.zeichnung?.cancel()
    this.seiten = []
    this.inhalt.replaceChildren()
    if (this.ladeauftrag) void this.ladeauftrag.destroy()
    this.ladeauftrag = null
    this.dokument = null
  }

  private berechneSkala(): void {
    const erste = this.seiten[0]
    if (!erste) return
    const verfuegbar = Math.max(240, this.flaeche.clientWidth - 2 * RAND)
    this.breiteZuletzt = this.flaeche.clientWidth
    this.skala = (Math.min(verfuegbar, MAX_BREITE) / erste.breite) * this.faktor
    // Textebene von pdf.js rechnet mit dieser Variable (Schriftgröße und Abmessungen je Seite)
    this.inhalt.style.setProperty('--total-scale-factor', String(this.skala))
  }

  private setzeGroesse(seite: Seite): void {
    seite.el.style.width = `${Math.floor(seite.breite * this.skala)}px`
    seite.el.style.height = `${Math.floor(seite.hoehe * this.skala)}px`
  }

  /** Größen neu setzen, Zeichnungen verwerfen, Stelle halten, sichtbare Seiten neu zeichnen. */
  private aufbauen(stelleHalten = false): void {
    const anker = stelleHalten ? this.stelle() : null
    this.berechneSkala()
    for (const s of this.seiten) {
      s.zeichnung?.cancel()
      s.zeichnung = null
      s.gezeichnet = false
      s.el.replaceChildren()
      this.setzeGroesse(s)
    }
    if (anker) {
      const seite = this.seiten[anker.nr - 1]
      if (seite) {
        const oben = seite.el.getBoundingClientRect().top - this.flaeche.getBoundingClientRect().top
        this.flaeche.scrollTop += oben + anker.anteil * seite.el.offsetHeight
      }
    }
    this.beobachten()
  }

  /** Aktuelle Seite und Anteil, wie weit sie oben aus der Fläche gescrollt ist. */
  private stelle(): { nr: number; anteil: number } {
    const seite = this.seiten[this.aktuell - 1]
    if (!seite) return { nr: 1, anteil: 0 }
    const oben = seite.el.getBoundingClientRect().top - this.flaeche.getBoundingClientRect().top
    const hoehe = seite.el.offsetHeight || 1
    return { nr: seite.nr, anteil: Math.min(1, Math.max(0, -oben / hoehe)) }
  }

  private beobachten(): void {
    this.beobachter?.disconnect()
    if (typeof IntersectionObserver === 'undefined') {
      // Ohne Beobachter (sehr alte Browser): die ersten Seiten sofort zeichnen
      for (const s of this.seiten.slice(0, BEHALTEN)) void this.zeichnen(s)
      return
    }
    this.beobachter = new IntersectionObserver(
      (eintraege) => {
        for (const e of eintraege) {
          if (!e.isIntersecting) continue
          const nr = Number((e.target as HTMLElement).dataset.seite)
          const seite = this.seiten[nr - 1]
          if (seite) void this.zeichnen(seite)
        }
      },
      { root: this.flaeche, rootMargin: '60% 0px' },
    )
    for (const s of this.seiten) this.beobachter.observe(s.el)
  }

  private async zeichnen(seite: Seite): Promise<void> {
    const dokument = this.dokument
    const pdfjs = this.pdfjs
    if (seite.gezeichnet || !dokument || !pdfjs) return
    seite.gezeichnet = true
    const lauf = this.lauf
    const skala = this.skala
    try {
      const page = await dokument.getPage(seite.nr)
      if (lauf !== this.lauf || skala !== this.skala || !seite.gezeichnet) return
      const basis = page.getViewport({ scale: 1 })
      if (basis.width !== seite.breite || basis.height !== seite.hoehe) {
        // Seiten mit anderem Format (Querformat, Karten) bekommen ihre eigene Größe
        seite.breite = basis.width
        seite.hoehe = basis.height
        this.setzeGroesse(seite)
      }
      const ansicht = page.getViewport({ scale: skala })
      const dichte = Math.min(window.devicePixelRatio || 1, 2)
      const canvas = document.createElement('canvas')
      canvas.width = Math.floor(ansicht.width * dichte)
      canvas.height = Math.floor(ansicht.height * dichte)
      canvas.setAttribute('aria-hidden', 'true')
      const zeichnung = page.render({
        canvas,
        viewport: ansicht,
        transform: dichte !== 1 ? [dichte, 0, 0, dichte, 0, 0] : undefined,
      })
      seite.zeichnung = zeichnung
      await zeichnung.promise
      seite.zeichnung = null
      if (lauf !== this.lauf || skala !== this.skala || !seite.gezeichnet) return
      const text = document.createElement('div')
      text.className = 'textLayer'
      seite.el.replaceChildren(canvas, text)
      const textebene = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container: text,
        viewport: ansicht,
      })
      await textebene.render()
      this.aufraeumen()
    } catch (err) {
      seite.zeichnung = null
      seite.gezeichnet = false
      if ((err as { name?: string } | null)?.name === 'RenderingCancelledException') return
      console.warn('Seite konnte nicht gezeichnet werden:', seite.nr, err)
    }
  }

  /** Canvas weit entfernter Seiten freigeben (Speicher bei langen Unterlagen). */
  private aufraeumen(): void {
    const gezeichnet = this.seiten.filter((s) => s.gezeichnet && !s.zeichnung)
    if (gezeichnet.length <= BEHALTEN) return
    gezeichnet
      .sort((a, b) => Math.abs(b.nr - this.aktuell) - Math.abs(a.nr - this.aktuell))
      .slice(0, gezeichnet.length - BEHALTEN)
      .forEach((s) => {
        s.gezeichnet = false
        s.el.replaceChildren()
        // Wieder zeichnen, sobald die Seite erneut in den Blick kommt
        this.beobachter?.unobserve(s.el)
        this.beobachter?.observe(s.el)
      })
  }

  private planeSeitenmeldung(): void {
    if (this.scrollPlan) return
    this.scrollPlan = window.requestAnimationFrame(() => {
      this.scrollPlan = 0
      this.meldeSeite()
    })
  }

  private meldeSeite(): void {
    if (!this.seiten.length) return
    const marke = this.flaeche.getBoundingClientRect().top + this.flaeche.clientHeight / 3
    let nr = 1
    for (const s of this.seiten) {
      if (s.el.getBoundingClientRect().top <= marke) nr = s.nr
      else break
    }
    if (nr !== this.aktuell) {
      this.aktuell = nr
      this.melden.seite?.(nr, this.seiten.length)
    }
  }

  private planeNeuaufbau(): void {
    if (!this.seiten.length) return
    if (Math.abs(this.flaeche.clientWidth - this.breiteZuletzt) < 8) return
    window.clearTimeout(this.resizePlan)
    this.resizePlan = window.setTimeout(() => this.aufbauen(true), 150)
  }
}
