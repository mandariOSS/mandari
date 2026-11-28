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
import type { SatzKonfig, Seitengeometrie, Seitenzahl } from './vertrag'
import { mmZuPx, seitenAnfang } from './vertrag'

const SVG_NS = 'http://www.w3.org/2000/svg'

interface Bild {
  url: string
  quelle: string
}

export class Boegen {
  readonly ebene: HTMLDivElement
  /** Gezeichnete Seiten des Briefpapier-PDFs (Index = Seite im PDF) */
  private bilder: (Bild | null)[] = []
  /** Seite des Briefpapiers je Seite des Dokuments (treu übernommenes PDF); leer: immer die erste */
  private zuordnung: number[] = []
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
    this.zuordnung = konfig.briefpapier.seiten ?? []
    if (url === this.bildQuelle && this.bildLaden) return this.bildLaden
    this.bildQuelle = url
    this.bilderFreigeben()
    if (!url) {
      this.bildLaden = Promise.resolve()
      return this.bildLaden
    }
    this.bildLaden = this.pdfZeichnen(url, g).catch((err) => {
      console.warn('Briefpapier konnte nicht gezeichnet werden:', err)
      // Beim nächsten Durchlauf erneut versuchen (etwa nach einem kurzen Netzaussetzer)
      this.bildQuelle = ''
      this.bildLaden = null
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
    // Der Server legt die erste Seite des Briefpapiers unter jede Seite, beim treu übernommenen PDF jede
    // Originalseite unter ihre Seite (overlay_pdf_letterhead): ebenso hier
    const benoetigt = new Set(this.zuordnung.length ? this.zuordnung : [0])
    const bilder: (Bild | null)[] = []
    for (const index of [...benoetigt].sort((a, b) => a - b)) {
      if (index >= dokument.numPages) continue
      bilder[index] = await zeichnen(index + 1)
    }
    this.bilder = bilder
    void dokument.cleanup()
  }

  private bilderFreigeben(): void {
    for (const bild of this.bilder) if (bild) URL.revokeObjectURL(bild.url)
    this.bilder = []
  }

  /** Bild des Briefpapiers für Seite `i` (0-basiert) */
  private bildFuer(i: number): Bild | null {
    if (!this.zuordnung.length) return this.bilder[0] ?? null
    const index = this.zuordnung[Math.min(i, this.zuordnung.length - 1)] ?? 0
    return this.bilder[index] ?? this.bilder.find(Boolean) ?? null
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
    if (konfig.titel) {
      const titel = document.createElement('p')
      titel.className = 'satz-kopf-titel'
      titel.textContent = konfig.titel
      kopf.appendChild(titel)
    }
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

  private inhalt(i: number, seiten: number, g: Seitengeometrie, konfig: SatzKonfig): Element[] {
    const teile: Element[] = []
    const bild = this.bildFuer(i)
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
    if (bp.seitenzahl && i + 1 >= bp.seitenzahl.ab_seite) teile.push(seitenzahlElement(bp.seitenzahl, i, seiten, g))
    return teile
  }

  entfernen(): void {
    this.bilderFreigeben()
    this.bildQuelle = ''
    this.bildLaden = null
    this.ebene.remove()
  }
}

/**
 * Seitenzahl eines treu übernommenen PDFs (#1600) an der Stelle des Originals: als SVG-Text, weil SVG genau auf der
 * Grundlinie setzt (im PDF bleibt sie Vektortext).
 */
function seitenzahlElement(zahl: Seitenzahl, i: number, seiten: number, g: Seitengeometrie): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('class', 'satz-seitenzahl-treu')
  svg.setAttribute('width', String(g.breite))
  svg.setAttribute('height', String(g.hoehe))
  svg.setAttribute('aria-hidden', 'true')
  const text = document.createElementNS(SVG_NS, 'text')
  const anker = { links: 'start', mitte: 'middle', rechts: 'end' }[zahl.ausrichtung]
  const x = { links: zahl.x_mm, mitte: zahl.mitte_mm, rechts: zahl.rechts_mm }[zahl.ausrichtung]
  text.setAttribute('x', String(mmZuPx(x)))
  text.setAttribute('y', String(mmZuPx(zahl.grund_mm)))
  text.setAttribute('text-anchor', anker)
  text.setAttribute('font-size', `${zahl.groesse_pt}pt`)
  text.setAttribute('font-weight', zahl.fett ? '700' : '400')
  text.style.fontFamily = zahl.stapel
  text.textContent = zahl.text.replace('{n}', String(i + 1)).replace('{gesamt}', String(seiten))
  svg.appendChild(text)
  return svg
}
