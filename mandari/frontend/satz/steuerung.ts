/**
 * Steuerung der Satz-Kette (Issue #1584): ein Durchlauf misst den Text, bricht ihn in Seiten um, setzt die Lücken
 * und zeichnet die Bögen. Läuft im Editor (Seitenansicht) und im Druck (frontend/satz/druck.ts) gleich.
 *
 * Ablauf eines Durchlaufs, alles in einer Aufgabe des Browsers (dazwischen zeichnet er nicht, es flackert nichts):
 * 1. Lücken entfernen (die Höhe des Stapels bleibt stehen, damit die Ansicht nicht springt)
 * 2. Blöcke und Zeilen in natürlicher Lage messen
 * 3. Seiten berechnen, Lücken setzen, Bögen zeichnen
 *
 * Durchläufe werden nach Änderungen kurz gesammelt (`planen`), während einer Eingabe über die Eingabehilfe (IME)
 * verschoben und bei nachgeladenen Schriften wiederholt.
 */

import type { Editor } from '@tiptap/core'
import { DecorationSet } from '@tiptap/pm/view'
import type { PdfJsModul } from '../editor/pdfjs-laden'
import { Boegen } from './boegen'
import { dekorationen, SATZ_KEY } from './erweiterung'
import { messen } from './messen'
import { familienImText, schriftenLaden } from './schriften'
import { umbrechen } from './umbruch'
import type { SatzKonfig, Seitengeometrie, Umbruch } from './vertrag'
import { geometrieAus, stapelHoehe } from './vertrag'

export interface SatzSteuerungOptionen {
  /** Abstand zwischen den Seiten in px (Druck: 0) */
  luecke: number
  druck?: boolean
  /** Nach jedem Durchlauf: Seitenzahl und Zahl der Blöcke, die über eine Seite hinausragen */
  onSeiten?: (seiten: number, zuHoch: number) => void
  /** Lädt pdf.js für ein Briefpapier als PDF (Editor); ohne bleibt das Briefpapier weg (Druck) */
  pdfjs?: () => Promise<PdfJsModul | null>
}

const SAMMELN_MS = 120

/** Bilder im Text laden lassen, bevor gemessen wird (sonst fehlt ihre Höhe); Fehler zählen als geladen */
function bilderLaden(text: HTMLElement): Promise<unknown> {
  const offen = Array.from(text.querySelectorAll('img')).filter((img) => !img.complete)
  return Promise.all(offen.map((img) => img.decode().catch(() => undefined)))
}

export class SatzSteuerung {
  private konfig: SatzKonfig | null = null
  private geometrie: Seitengeometrie | null = null
  private boegen: Boegen | null = null
  private plan = 0
  private laeuft: Promise<Umbruch | null> | null = null
  private nochmal = false
  private skala = 1
  private eingeschaltet = false
  seiten = 1
  private readonly beiAenderung = ({ transaction }: { transaction: { docChanged: boolean } }) => {
    if (transaction.docChanged) this.planen()
  }
  private readonly beiSchrift = () => this.planen()
  private readonly beiGroesse = () => this.planen()
  /** Ein Bild ohne feste Höhe ändert beim Laden den Satz */
  private readonly beiBild = (event: Event) => {
    const ziel = event.target as Element | null
    // Nur Bilder im Text: Briefpapier und Logo in den Bögen entstehen bei jedem Lauf neu (sonst eine Schleife)
    if (ziel?.tagName === 'IMG' && this.eingehaengt() && this.text.contains(ziel)) this.planen()
  }

  constructor(
    private readonly editor: Editor,
    /** Element um den Text (#editor-container); trägt die Bögen */
    private readonly rahmen: HTMLElement,
    private readonly optionen: SatzSteuerungOptionen,
  ) {}

  get aktiv(): boolean {
    return this.konfig !== null
  }

  get text(): HTMLElement {
    return this.editor.view.dom as HTMLElement
  }

  /** Ist die Ansicht des Editors eingehängt? (TipTap 3 hängt sie erst nach dem Erzeugen ein) */
  private eingehaengt(): boolean {
    try {
      return !this.editor.isDestroyed && !!this.editor.view.dom
    } catch {
      return false
    }
  }

  private bereit(): Promise<void> {
    if (this.eingehaengt()) return Promise.resolve()
    return new Promise((fertig) => {
      const beiCreate = () => {
        this.editor.off('create', beiCreate)
        fertig()
      }
      this.editor.on('create', beiCreate)
    })
  }

  /** Satz einschalten bzw. umstellen (Briefpapier, Schrift); `null` schaltet ihn ab */
  async setzen(konfig: SatzKonfig | null): Promise<Umbruch | null> {
    this.konfig = konfig
    if (!konfig) {
      if (this.eingeschaltet) this.abschalten()
      return null
    }
    await this.bereit()
    if (this.konfig !== konfig || this.editor.isDestroyed) return null
    if (!this.eingeschaltet) {
      this.eingeschaltet = true
      this.editor.on('transaction', this.beiAenderung)
      document.fonts?.addEventListener('loadingdone', this.beiSchrift)
      window.addEventListener('resize', this.beiGroesse)
      // load steigt nicht auf: im Erfassen hören
      this.rahmen.addEventListener('load', this.beiBild, true)
      this.boegen = new Boegen(this.rahmen, !!this.optionen.druck, this.optionen.pdfjs)
      this.rahmen.classList.add('satz-aktiv')
      this.text.classList.add('dokument-satz')
    }
    // Treu übernommenes PDF (#1600): ohne Unterschneidung und Ligaturen, wie Word setzt
    this.text.classList.toggle('satz-treu', !!konfig.treu)
    this.text.classList.toggle('satz-treu-unterschneidung', !!konfig.treu && !!konfig.treu_unterschneidung)
    // Am Rahmen: Text und Bögen (Fußzeilen) erben Schrift und Größe
    const s = this.rahmen.style
    s.setProperty('--satz-schrift', konfig.schrift.stapel)
    s.setProperty('--satz-groesse', `${konfig.schrift.groesse_pt}pt`)
    s.setProperty('--satz-zeilenabstand', String(konfig.schrift.zeilenabstand))
    return this.jetzt()
  }

  private abschalten(): void {
    this.eingeschaltet = false
    this.editor.off('transaction', this.beiAenderung)
    document.fonts?.removeEventListener('loadingdone', this.beiSchrift)
    window.removeEventListener('resize', this.beiGroesse)
    this.rahmen.removeEventListener('load', this.beiBild, true)
    window.clearTimeout(this.plan)
    this.lueckenSetzen(DecorationSet.empty)
    this.boegen?.entfernen()
    this.boegen = null
    this.geometrie = null
    this.rahmen.classList.remove('satz-aktiv')
    this.text.classList.remove('dokument-satz', 'satz-treu', 'satz-treu-unterschneidung')
    for (const name of ['--satz-schrift', '--satz-groesse', '--satz-zeilenabstand'])
      this.rahmen.style.removeProperty(name)
    this.text.style.removeProperty('padding')
    this.rahmen.style.removeProperty('width')
    this.rahmen.style.removeProperty('min-height')
    this.zoomAnwenden()
  }

  /** Zoom der Ansicht (1 = 100 %); ändert nur die Darstellung, nicht den Satz */
  setzeSkala(skala: number): void {
    this.skala = skala
    this.zoomAnwenden()
  }

  private zoomAnwenden(): void {
    const huelle = this.rahmen.parentElement
    if (!this.konfig || !this.geometrie || this.skala === 1) {
      this.rahmen.style.removeProperty('transform')
      this.rahmen.style.removeProperty('transform-origin')
    } else {
      this.rahmen.style.transform = `scale(${this.skala})`
      this.rahmen.style.transformOrigin = 'top left'
    }
    if (huelle?.classList.contains('satz-huelle')) {
      if (this.konfig && this.geometrie) {
        huelle.style.width = `${this.geometrie.breite * this.skala}px`
        huelle.style.height = `${stapelHoehe(this.geometrie, this.seiten) * this.skala}px`
      } else {
        huelle.style.removeProperty('width')
        huelle.style.removeProperty('height')
      }
    }
  }

  planen(ms = SAMMELN_MS): void {
    if (!this.konfig) return
    window.clearTimeout(this.plan)
    this.plan = window.setTimeout(() => void this.jetzt(), ms)
  }

  /** Durchlauf sofort (oder direkt nach dem laufenden); liefert sein Ergebnis */
  async jetzt(): Promise<Umbruch | null> {
    window.clearTimeout(this.plan)
    if (this.laeuft) {
      this.nochmal = true
      return this.laeuft
    }
    this.laeuft = this.durchlauf()
    try {
      return await this.laeuft
    } finally {
      this.laeuft = null
      if (this.nochmal) {
        this.nochmal = false
        this.planen(0)
      }
    }
  }

  private async durchlauf(): Promise<Umbruch | null> {
    const konfig = this.konfig
    if (!konfig || !this.eingehaengt()) return null
    if (this.editor.view.composing) {
      this.planen(400)
      return null
    }
    const vorlaeufig = geometrieAus(konfig, this.optionen.luecke)
    await Promise.all([
      schriftenLaden([...konfig.schriften.map((s) => s.name), ...familienImText(this.text)]),
      this.boegen?.vorbereiten(konfig, vorlaeufig),
      bilderLaden(this.text),
    ])
    if (this.konfig !== konfig || this.editor.isDestroyed || !this.boegen) return null

    // Satzspiegel der ersten Seite nicht über dem generierten Kopf beginnen lassen
    const g = geometrieAus(konfig, this.optionen.luecke, this.boegen.kopfUnten(konfig, vorlaeufig))
    this.geometrie = g
    const text = this.text
    this.rahmen.style.width = `${g.breite}px`
    text.style.padding = `${g.erste.oben}px ${g.rechts}px 0 ${g.links}px`
    // Gemessen wird im tatsächlichen Zoom (transform): erst anwenden, dann messen und durch die Skala teilen
    this.zoomAnwenden()

    // 1. Lücken entfernen; die bisherige Höhe hält die Ansicht (Scroll-Stelle) fest
    this.rahmen.style.minHeight = `${stapelHoehe(g, this.seiten)}px`
    this.lueckenSetzen(DecorationSet.empty)
    // 2. Messen (erzwingt das Layout ohne Lücken), 3. umbrechen und Lücken setzen
    const bloecke = messen(this.editor.view, text, this.skala)
    const ergebnis = umbrechen(bloecke, g)
    this.lueckenSetzen(dekorationen(this.editor.state.doc, ergebnis.luecken))
    this.seiten = ergebnis.seiten
    this.rahmen.style.minHeight = `${stapelHoehe(g, ergebnis.seiten)}px`
    this.boegen.zeichnen(ergebnis.seiten, g, konfig)
    this.zoomAnwenden()
    this.optionen.onSeiten?.(ergebnis.seiten, ergebnis.zuHoch)
    return ergebnis
  }

  private lueckenSetzen(luecken: DecorationSet): void {
    if (!this.eingehaengt()) return
    const view = this.editor.view
    const tr = view.state.tr.setMeta(SATZ_KEY, { luecken }).setMeta('addToHistory', false)
    view.dispatch(tr)
  }

  /** Seite (1-basiert), die an der senkrechten Bildschirmposition `y` liegt */
  seiteBei(y: number): number {
    const g = this.geometrie
    if (!g) return 1
    const oben = (y - this.rahmen.getBoundingClientRect().top) / this.skala
    return Math.min(this.seiten, Math.max(1, Math.floor(oben / (g.hoehe + g.luecke)) + 1))
  }

  /** Zur Seite `nr` (1-basiert) scrollen */
  zurSeite(nr: number): void {
    const bogen = this.boegen?.ebene.children[nr - 1] as HTMLElement | undefined
    bogen?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  beenden(): void {
    this.konfig = null
    if (this.eingeschaltet && this.eingehaengt()) this.abschalten()
    else this.boegen?.entfernen()
  }
}
