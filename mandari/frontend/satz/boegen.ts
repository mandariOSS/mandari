/**
 * Seitenbögen der Satz-Kette (Issue #1584): die Seiten hinter dem Text.
 *
 * Je Seite ein Bogen in Seitengröße an seiner Stelle im Stapel. Er trägt das Briefpapier und, beim generierten
 * Briefkopf, den Kopf (nur Seite 1), die Fußzeilen und „Seite x von y“. Der Text darüber ist durchsichtig; die
 * Bögen nehmen keine Eingaben an.
 *
 * Briefpapier als PDF: pdf.js zeichnet die erste Seite (und, falls vorhanden, die zweite für die Folgeseiten) einmal
 * als Bild; alle Bögen nutzen es. Im Druck bleibt das Briefpapier weg: Der Server legt es als Vektor-Hintergrund unter
 * das fertige PDF (apps/common/pdf.py).
 */

import type { PdfJsModul } from '../editor/pdfjs-laden'
import type { SatzKonfig, Seitengeometrie } from './vertrag'
import { mmZuPx, seitenAnfang } from './vertrag'

interface Bild {
  url: string
  quelle: string
}

export class Boegen {
  readonly ebene: HTMLDivElement
  private bilder: { erste: Bild | null; folge: Bild | null } = { erste: null, folge: null }
  private bildLaden: Promise<void> | null = null
  private bildQuelle = ''

  constructor(
    private readonly rahmen: HTMLElement,
    private readonly druck: boolean,
    /** Lädt pdf.js für das Briefpapier (nur im Editor; der Druck bündelt kein pdf.js) */
    private readonly pdfjsLaden?: () => Promise<PdfJsModul | null>,
  ) {
    this.ebene = document.createElement('div')
    this.ebene.className = 'satz-boegen'
    this.ebene.setAttribute('aria-hidden', 'true')
    rahmen.insertBefore(this.ebene, rahmen.firstChild)
  }

  /** Briefpapier-PDF als Bild vorbereiten (nur im Editor) */
  vorbereiten(konfig: SatzKonfig, g: Seitengeometrie): Promise<void> {
    const url = this.druck || !this.pdfjsLaden || konfig.briefpapier.art !== 'pdf' ? '' : konfig.briefpapier.pdf_url
    if (url === this.bildQuelle && this.bildLaden) return this.bildLaden
    this.bildQuelle = url
    this.bilderFreigeben()
    if (!url) {
      this.bildLaden = Promise.resolve()
      return this.bildLaden
    }
    this.bildLaden = this.pdfZeichnen(url, g).catch((err) => {
      console.warn('Briefpapier konnte nicht gezeichnet werden:', err)
    })
    return this.bildLaden
  }

  private async pdfZeichnen(url: string, g: Seitengeometrie): Promise<void> {
    const pdfjs = await this.pdfjsLaden?.()
    if (!pdfjs) return
    const dokument = await pdfjs.getDocument({ url }).promise
    // Scharf auch bei Zoom über 100 % und auf hochauflösenden Bildschirmen, aber höchstens doppelt
    const dichte = Math.min(2, (window.devicePixelRatio || 1) * 1.5)
    const zeichnen = async (nr: number): Promise<Bild | null> => {
      const seite = await dokument.getPage(nr)
      const basis = seite.getViewport({ scale: 1 })
      const ansicht = seite.getViewport({ scale: (g.breite * dichte) / basis.width })
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(ansicht.width)
      canvas.height = Math.round(ansicht.height)
      const ctx = canvas.getContext('2d')
      if (!ctx) return null
      await seite.render({ canvas, canvasContext: ctx, viewport: ansicht }).promise
      const blob = await new Promise<Blob | null>((fertig) => canvas.toBlob(fertig, 'image/png'))
      return blob ? { url: URL.createObjectURL(blob), quelle: url } : null
    }
    const erste = await zeichnen(1)
    // Der Server legt die erste Seite des Briefpapiers unter jede Seite (overlay_pdf_letterhead): ebenso hier
    this.bilder = { erste, folge: erste }
    void dokument.cleanup()
  }

  private bilderFreigeben(): void {
    const urls = new Set([this.bilder.erste?.url, this.bilder.folge?.url].filter(Boolean) as string[])
    for (const url of urls) URL.revokeObjectURL(url)
    this.bilder = { erste: null, folge: null }
  }

  /**
   * Höhe des Kopfs der ersten Seite (generierter Briefkopf) in px ab Seitenoberkante; 0 ohne Kopf. Der Satzspiegel
   * der ersten Seite beginnt nicht darüber.
   */
  kopfUnten(konfig: SatzKonfig, g: Seitengeometrie): number {
    if (konfig.briefpapier.art !== 'generiert' || !konfig.briefpapier.kopf_html) return 0
    const kopf = this.kopfElement(konfig, g)
    this.ebene.appendChild(kopf)
    const unten = kopf.offsetTop + kopf.offsetHeight
    kopf.remove()
    return unten + mmZuPx(4)
  }

  private kopfElement(konfig: SatzKonfig, g: Seitengeometrie): HTMLElement {
    const kopf = document.createElement('div')
    kopf.className = 'satz-kopf'
    kopf.style.left = `${g.links}px`
    kopf.style.right = `${g.rechts}px`
    if (konfig.briefpapier.akzent) kopf.style.setProperty('--satz-akzent', konfig.briefpapier.akzent)
    // Vom Server aus Feldern der Organisation gerendert (Template mit Escaping), keine Eingabe aus dem Text
    kopf.innerHTML = konfig.briefpapier.kopf_html
    return kopf
  }

  zeichnen(seiten: number, g: Seitengeometrie, konfig: SatzKonfig): void {
    const vorhanden = Array.from(this.ebene.children) as HTMLElement[]
    for (const alt of vorhanden.slice(seiten)) alt.remove()
    for (let i = 0; i < seiten; i++) {
      let bogen = this.ebene.children[i] as HTMLElement | undefined
      if (!bogen) {
        bogen = document.createElement('div')
        bogen.className = 'satz-bogen'
        this.ebene.appendChild(bogen)
      }
      bogen.style.top = `${seitenAnfang(g, i)}px`
      bogen.style.width = `${g.breite}px`
      bogen.style.height = `${g.hoehe}px`
      bogen.dataset.seite = String(i + 1)
      bogen.replaceChildren(...this.inhalt(i, seiten, g, konfig))
    }
  }

  private inhalt(i: number, seiten: number, g: Seitengeometrie, konfig: SatzKonfig): HTMLElement[] {
    const teile: HTMLElement[] = []
    const bild = i === 0 ? this.bilder.erste : this.bilder.folge
    if (bild) {
      const img = document.createElement('img')
      img.className = 'satz-briefpapier'
      img.src = bild.url
      img.alt = ''
      img.draggable = false
      teile.push(img)
    }
    const bp = konfig.briefpapier
    if (bp.art === 'generiert' && i === 0 && bp.kopf_html) teile.push(this.kopfElement(konfig, g))
    if (bp.fuss_zeilen.length || bp.seitenzahlen) {
      const fuss = document.createElement('div')
      fuss.className = 'satz-fuss'
      fuss.style.left = `${g.links}px`
      fuss.style.right = `${g.rechts}px`
      const unten = i === 0 ? g.erste.unten : g.folge.unten
      fuss.style.bottom = `${Math.max(mmZuPx(6), unten - mmZuPx(20))}px`
      for (const text of bp.fuss_zeilen) {
        const zeile = document.createElement('div')
        zeile.textContent = text
        fuss.appendChild(zeile)
      }
      if (bp.seitenzahlen) {
        const nr = document.createElement('div')
        nr.className = 'satz-seitenzahl'
        nr.textContent = `Seite ${i + 1} von ${seiten}`
        fuss.appendChild(nr)
      }
      teile.push(fuss)
    }
    return teile
  }

  entfernen(): void {
    this.bilderFreigeben()
    this.bildQuelle = ''
    this.bildLaden = null
    this.ebene.remove()
  }
}
