/**
 * Sitzungsvorbereitung im neuen Design (Alpine-Komponente `vorbereitung`, #856).
 *
 * Baut auf `preparationApp` auf (frontend/alpine/prepare-meeting.ts): Speichern, Position, Notizen,
 * Diskussion mit Echtzeit, Redebeitrag, Anlagen und Anmerkungen sind dieselben Methoden und Endpunkte
 * (#854 ersetzt die Speicherlogik später an einer Stelle für beide Ansichten). Hier kommt nur hinzu, was
 * die neue Anordnung braucht:
 *
 * - Unterlagen des TOPs als Reiter (Vorlage, Anlagen, eigene Anlagen; Überlauf „n weitere“) mit
 *   Blattansicht über pdf.js und Schriftgröße A−/A+ (frontend/alpine/pdf-blatt.ts)
 * - Reiter der Arbeitsspalte (Begründung, Notiz, Diskussion, Redebeitrag), gemerkt im Browser
 * - Leiste unten: Position, voriger und nächster TOP, Stand der Vorbereitung
 * - Tagesordnung als Schublade bzw. Blatt, Unterlagen und Position als Blatt am Handy
 * - Sprungmarke `#top-<id>` in der Adresse (Neuladen bleibt beim TOP)
 *
 * Markup: templates/work/meetings/vorbereitung/ (seite.html und Partials). Konfiguration wie bisher aus
 * `{{ prepare_config|json_script:"prepare-config" }}`.
 */

import type { Magics, XDataContext } from 'alpinejs'
import { type BlattZustand, PdfBlatt } from './pdf-blatt'
import { type PreparedFile, type PreparedItem, preparationApp, type SupplementaryDoc } from './prepare-meeting'

// ---- Typen ---------------------------------------------------------------------------

/** Eine Unterlage des TOPs: Datei aus dem RIS oder eigene Anlage (Datei bzw. Link) */
export interface Unterlage {
  schluessel: string
  art: 'ris' | 'eigen'
  name: string
  istPdf: boolean
  istLink: boolean
  /** Adresse für die Blattansicht (gleiche Herkunft), leer ohne Vorschau */
  vorschau: string
  /** Adresse zum Öffnen im neuen Tab */
  oeffnen: string
  /** Anker der Anmerkungen: `oparl` (RIS-Datei) bzw. `doc` (eigene Anlage) */
  ankerArt: string | undefined
  ankerId: string | undefined
  /** Träger des Anmerkungs-Zählers */
  quelle: PreparedFile | SupplementaryDoc
  meta: string
  /** Eigene Anlage: wer sie angehängt hat, ob sie über die Vorlage geteilt ist, ob sie entfernt werden darf */
  eigene: SupplementaryDoc | null
}

export type Reiter = 'begruendung' | 'notiz' | 'diskussion' | 'rede'
export type Blatt = '' | 'tagesordnung' | 'unterlagen' | 'position'

interface Gruppe {
  titel: string
  items: PreparedItem[]
}

// ---- Konstanten ----------------------------------------------------------------------

/** Zoomfaktoren relativ zur Seitenbreite; Stufe 3 = Seitenbreite */
export const ZOOM = [0.6, 0.75, 0.9, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.5]
const ZOOM_NORMAL = 3
const SPEICHER_ZOOM = 'mandari.vorbereitung.zoom'
const SPEICHER_REITER = 'mandari.vorbereitung.reiter'
const REITER: Reiter[] = ['begruendung', 'notiz', 'diskussion', 'rede']
/** Unter dieser Breite der Fläche (CSS: 60rem) gilt die Handy-Anordnung: ein TOP je Bildschirm */
const SCHMAL = 960
/** Weitere Positionen hinter „Andere …“ (vier gleichrangige Werte stehen als Knöpfe in der Leiste) */
export const ANDERE_POSITIONEN = ['defer', 'refer', 'amended', 'info']
/** Ungefähre Breite eines Unterlagen-Reiters für die Berechnung des Überlaufs */
const REITER_BREITE = 168

/** Farbklasse des Positionspunkts (immer zusammen mit dem Text) */
export function positionsKlasse(code: string | undefined): string {
  if (code === 'for') return 'zustimmung'
  if (code === 'against') return 'ablehnung'
  if (code === 'abstain') return 'enthaltung'
  if (!code || code === 'open') return 'offen'
  return 'andere'
}

function lesen(schluessel: string): string | null {
  try {
    return window.localStorage.getItem(schluessel)
  } catch {
    return null
  }
}

function merken(schluessel: string, wert: string): void {
  try {
    window.localStorage.setItem(schluessel, wert)
  } catch {
    /* privater Modus: ohne Merken weiter */
  }
}

/** Unterlagen eines TOPs: erst die Dateien aus dem RIS, dann die eigenen Anlagen */
export function unterlagenFuer(item: PreparedItem | null, docs: SupplementaryDoc[]): Unterlage[] {
  if (!item) return []
  const ris: Unterlage[] = (item.files || []).map((f) => ({
    schluessel: `ris:${f.id}`,
    art: 'ris',
    name: f.name,
    istPdf: f.isPdf,
    istLink: false,
    vorschau: f.isPdf ? f.previewUrl : '',
    oeffnen: f.url,
    ankerArt: 'oparl',
    ankerId: f.id,
    quelle: f,
    meta: ['Aus dem Ratsinformationssystem', f.isPdf ? 'PDF' : '', f.size].filter(Boolean).join(', '),
    eigene: null,
  }))
  const eigene: Unterlage[] = docs.map((d) => {
    const istPdf = !!d.is_pdf
    const istLink = d.document_type === 'link'
    return {
      schluessel: `eigen:${d.id}`,
      art: 'eigen',
      name: d.title,
      istPdf,
      istLink,
      vorschau: istPdf ? String(d.preview_url || '') : '',
      oeffnen: String(d.url || ''),
      ankerArt: d.preview_kind,
      ankerId: d.preview_id,
      quelle: d,
      meta: [istLink ? 'Link' : 'Eigene Anlage', d.added_by ? `von ${d.added_by}` : ''].filter(Boolean).join(' '),
      eigene: d,
    }
  })
  return [...ris, ...eigene]
}

/**
 * Objekte zusammenführen, ohne Getter auszuwerten (Spread würde `filteredItems` usw. einfrieren).
 * Die Methoden von `erweiterung` sehen per ThisType alle Felder beider Teile.
 */
function erweitere<B extends object, E extends object>(
  basis: B,
  erweiterung: E & ThisType<B & E & XDataContext & Magics<B & E>>,
): B & E {
  const ziel = {}
  Object.defineProperties(ziel, Object.getOwnPropertyDescriptors(basis))
  Object.defineProperties(ziel, Object.getOwnPropertyDescriptors(erweiterung))
  return ziel as B & E
}

// ---- Komponente ----------------------------------------------------------------------

/** Alpine-Komponente: `x-data="vorbereitung"` */
export function vorbereitung() {
  const basis = preparationApp()
  // Methoden der Grundkomponente, die hier erweitert werden
  const basisSelectItem = basis.selectItem
  const basisLoadDocs = basis.loadDocs
  const basisKeydown = basis.handleKeydown
  const basisAddLink = basis.addLink
  const basisUploadFile = basis.uploadFile
  const basisDeleteDoc = basis.deleteDoc

  // Außerhalb der Reaktivität: pdf.js-Ansicht und Wurzelelement
  let blatt: PdfBlatt | null = null
  let wurzel: HTMLElement | null = null
  let reiterBeobachter: ResizeObserver | null = null

  const gespeicherterZoom = Number.parseInt(lesen(SPEICHER_ZOOM) || '', 10)
  const gespeicherterReiter = lesen(SPEICHER_REITER) as Reiter | null

  return erweitere(basis, {
    unterlageIndex: 0,
    _gezeigt: '',
    zoomStufe: Number.isInteger(gespeicherterZoom)
      ? Math.min(ZOOM.length - 1, Math.max(0, gespeicherterZoom))
      : ZOOM_NORMAL,
    zoomMax: ZOOM.length - 1,
    blattZustand: '' as '' | BlattZustand,
    seite: 1,
    seitenzahl: 0,
    anmerkungenOffen: false,
    reiter: (gespeicherterReiter && REITER.includes(gespeicherterReiter) ? gespeicherterReiter : 'notiz') as Reiter,
    blatt: '' as Blatt,
    kopfOffen: false,
    menueOffen: false,
    weitereOffen: false,
    sitzungsnotizenOffen: false,
    anlageOffen: false,
    sichtbareReiter: 3,
    andereCodes: ANDERE_POSITIONEN,

    // ---------- Berechnet ----------
    get unterlagen(): Unterlage[] {
      return unterlagenFuer(this.selectedItem, this.docs)
    },
    get aktiveUnterlage(): Unterlage | null {
      return this.unterlagen[this.unterlageIndex] || null
    },
    get sichtbareUnterlagen(): Unterlage[] {
      const alle = this.unterlagen
      // Passt alles bis auf eine, zeigt „1 weitere“ nicht mehr Platz als der Reiter selbst
      const zahl = alle.length <= this.sichtbareReiter + 1 ? alle.length : this.sichtbareReiter
      return alle.slice(0, zahl)
    },
    get weitereUnterlagen(): Unterlage[] {
      return this.unterlagen.slice(this.sichtbareUnterlagen.length)
    },
    get aktiveIstWeitere(): boolean {
      return this.unterlageIndex >= this.sichtbareUnterlagen.length
    },
    get istAndere(): boolean {
      return !!this.selectedItem && ANDERE_POSITIONEN.includes(this.selectedItem.position)
    },
    /** Nur TOPs mit Vorlage zählen („2 von 9 Vorlagen mit Position“); ohne Vorlagen alle TOPs */
    get standText(): string {
      const mitVorlage = this.items.filter((i) => i.paper)
      if (mitVorlage.length === 0) {
        return `${this.positionedCount} von ${this.items.length} TOPs mit Position`
      }
      const gesetzt = mitVorlage.filter((i) => i.position && i.position !== 'open').length
      return `${gesetzt} von ${mitVorlage.length} Vorlagen mit Position`
    },
    get standKurz(): string {
      const mitVorlage = this.items.filter((i) => i.paper)
      const basis = mitVorlage.length ? mitVorlage : this.items
      const gesetzt = basis.filter((i) => i.position && i.position !== 'open').length
      return `${gesetzt} von ${basis.length} mit Position`
    },
    /** Kurzer Speicherstand für den Kopf; Fehler bleiben ausführlich (ganze Meldung im Tooltip) */
    get speicherText(): string {
      if (this.pendingSaves > 0) return 'Speichert …'
      if (this.saveError) return 'Nicht gespeichert'
      if (this.lastSavedAt) return `Gespeichert ${this.lastSavedAt}`
      return 'Automatisch gespeichert'
    },
    get topIndex(): number {
      return this.items.findIndex((i) => i.id === this.selectedItemId)
    },
    get vorigerTop(): PreparedItem | null {
      return this.topIndex > 0 ? this.items[this.topIndex - 1] : null
    },
    get naechsterTop(): PreparedItem | null {
      const i = this.topIndex
      return i >= 0 && i < this.items.length - 1 ? this.items[i + 1] : null
    },
    get gruppen(): Gruppe[] {
      const liste = this.filteredItems
      if (liste.every((i) => i.isPublic !== false)) return [{ titel: '', items: liste }]
      return [
        { titel: 'Öffentlicher Teil', items: liste.filter((i) => i.isPublic !== false) },
        { titel: 'Nichtöffentlicher Teil', items: liste.filter((i) => i.isPublic === false) },
      ].filter((g) => g.items.length > 0)
    },
    /** Beratungsfolge der Vorlage (alle Gremien, die sie beraten) */
    get beratungsfolge(): Array<Record<string, unknown>> {
      return (this.selectedItem?.paper?.consultations || []) as Array<Record<string, unknown>>
    },
    /** Rolle dieser Sitzung in der Beratungsfolge (z. B. „Entscheidung“) */
    get rolleHier(): string {
      const hier = this.beratungsfolge.find((c) => c.isCurrent)
      return hier ? String(hier.role || '') : ''
    },
    get verlauf(): Array<Record<string, unknown>> {
      return (this.selectedItem?.crossPositions || []) as Array<Record<string, unknown>>
    },
    /** Kopf ausklappbar, wenn es mehr als eine Zeile Verlauf oder eine Beratungsfolge gibt */
    get kopfHatMehr(): boolean {
      return this.verlauf.length > 1 || this.beratungsfolge.length > 1
    },

    // ---------- Start ----------
    init(): void {
      wurzel = this.$el
      // Wie preparationApp.init, aber mit Sprungmarke: Neuladen bleibt beim TOP
      const ziel = this.topAusAdresse()
      if (ziel) this.selectItem(ziel)
      else if (this.items.length > 0) this.selectItem(this.items[0].id)
      window.addEventListener('beforeunload', () => this.teardownRealtime())
      this.beobachteReiterleiste()
    },

    destroy(): void {
      blatt?.beenden()
      blatt = null
      reiterBeobachter?.disconnect()
      this.teardownRealtime()
    },

    topAusAdresse(): string | null {
      const treffer = /^#top-([0-9a-f-]{36})$/i.exec(window.location.hash)
      if (!treffer) return null
      return this.items.some((i) => i.id === treffer[1]) ? treffer[1] : null
    },

    istSchmal(): boolean {
      return !!wurzel && wurzel.clientWidth < SCHMAL
    },

    // ---------- TOP-Auswahl ----------
    selectItem(itemId: string): void {
      const wechsel = this.selectedItemId !== itemId
      basisSelectItem.call(this, itemId)
      if (!wechsel) return
      this.unterlageIndex = 0
      this._gezeigt = ''
      this.kopfOffen = false
      this.weitereOffen = false
      if (this.blatt === 'tagesordnung' || this.blatt === 'position') this.blatt = ''
      this.zeigeUnterlage()
      try {
        window.history.replaceState(null, '', `#top-${itemId}`)
      } catch {
        /* ohne Sprungmarke weiter */
      }
    },

    zuTop(item: PreparedItem | null): void {
      if (item) this.selectItem(item.id)
    },

    handleKeydown(e: KeyboardEvent): void {
      const ziel = e.target as HTMLElement | null
      // In der Blattansicht scrollen die Pfeiltasten das Dokument
      if (ziel?.closest?.('[data-eigene-tasten]')) return
      if (this.blatt || this.sitzungsnotizenOffen || this.anlageOffen || this.menueOffen || this.weitereOffen) return
      basisKeydown.call(this, e)
    },

    // ---------- Unterlagen ----------
    async loadDocs(): Promise<void> {
      await basisLoadDocs.call(this)
      // Kamen eigene Anlagen dazu, zeigt die Fläche die gewählte (bei TOPs ohne RIS-Dateien die erste)
      this.zeigeUnterlage()
    },

    waehleUnterlage(index: number): void {
      this.unterlageIndex = index
      this.weitereOffen = false
      this.zeigeUnterlage()
    },

    /** Unterlage am Handy als Blatt öffnen */
    unterlageAlsBlatt(index: number): void {
      this.blatt = 'unterlagen'
      this.waehleUnterlage(index)
    },

    zeigeUnterlage(): void {
      if (this.unterlageIndex >= this.unterlagen.length) this.unterlageIndex = 0
      const u = this.aktiveUnterlage
      // Am Handy lädt die Unterlage erst, wenn das Blatt offen ist (kein Download je TOP-Wechsel)
      const aufgeschoben = this.istSchmal() && this.blatt !== 'unterlagen'
      const schluessel = u && !aufgeschoben ? u.schluessel : ''
      if (schluessel === this._gezeigt) return
      this._gezeigt = schluessel
      if (!u || aufgeschoben || !u.istPdf || !u.vorschau) {
        blatt?.schliessen()
        this.blattZustand = ''
        this.seitenzahl = 0
        this.closePreview()
        return
      }
      this.openPreview(u.ankerArt, u.ankerId, u.name, u.vorschau, u.oeffnen, u.quelle)
      void this.$nextTick(() => {
        const flaeche = this.$refs.blatt
        if (!flaeche) return
        if (!blatt) {
          blatt = new PdfBlatt(flaeche, {
            seite: (seite, seiten) => {
              this.seite = seite
              this.seitenzahl = seiten
              // Neue Anmerkung bezieht sich auf die sichtbare Seite, solange dort nicht getippt wird
              const aktiv = document.activeElement
              if (!aktiv?.closest?.('#anmerkung-neu')) this.newAnnotationPage = seite
            },
            zustand: (zustand) => {
              this.blattZustand = zustand
            },
          })
        }
        void blatt.oeffnen(u.vorschau, ZOOM[this.zoomStufe])
      })
    },

    zoomen(richtung: number): void {
      const stufe = Math.min(ZOOM.length - 1, Math.max(0, this.zoomStufe + richtung))
      if (stufe === this.zoomStufe) return
      this.zoomStufe = stufe
      merken(SPEICHER_ZOOM, String(stufe))
      blatt?.zoom(ZOOM[stufe])
    },

    zoomZurueck(): void {
      this.zoomen(ZOOM_NORMAL - this.zoomStufe)
    },

    // Seitensprung der Anmerkungen: in der Blattansicht scrollen statt die Vorschau neu zu laden
    previewGotoPage(page: number | string): void {
      const nr = Math.max(1, Number.parseInt(String(page), 10) || 1)
      this.newAnnotationPage = nr
      blatt?.zurSeite(nr)
    },

    async addLink(): Promise<void> {
      const vorher = this.docs.length
      await basisAddLink.call(this)
      if (this.docs.length > vorher) this.nachHinzufuegen()
    },

    async uploadFile(event: Event): Promise<void> {
      const vorher = this.docs.length
      await basisUploadFile.call(this, event)
      if (this.docs.length > vorher) this.nachHinzufuegen()
    },

    nachHinzufuegen(): void {
      this.anlageOffen = false
      this.attachToPaper = false
      this.waehleUnterlage(this.unterlagen.length - 1)
    },

    async deleteDoc(doc: SupplementaryDoc): Promise<void> {
      await basisDeleteDoc.call(this, doc)
      if (!this.docs.some((d) => d.id === doc.id)) {
        this.unterlageIndex = Math.min(this.unterlageIndex, Math.max(0, this.unterlagen.length - 1))
        this._gezeigt = ''
        this.zeigeUnterlage()
      }
    },

    /** Pfeiltasten zwischen den sichtbaren Unterlagen-Reitern (Muster „Tabs“) */
    unterlagenTaste(e: KeyboardEvent): void {
      const letzte = this.sichtbareUnterlagen.length - 1
      const jetzt = Math.min(this.unterlageIndex, letzte)
      let neu = jetzt
      if (e.key === 'ArrowRight') neu = Math.min(letzte, jetzt + 1)
      else if (e.key === 'ArrowLeft') neu = Math.max(0, jetzt - 1)
      else if (e.key === 'Home') neu = 0
      else if (e.key === 'End') neu = letzte
      else return
      e.preventDefault()
      e.stopPropagation()
      this.waehleUnterlage(neu)
      void this.$nextTick(() => document.getElementById(`unterlage-${neu}`)?.focus())
    },

    /** Anzahl sichtbarer Unterlagen-Reiter aus der Breite der Leiste */
    beobachteReiterleiste(): void {
      const leiste = this.$refs.unterlagenleiste
      if (!leiste || typeof ResizeObserver === 'undefined') return
      reiterBeobachter = new ResizeObserver(() => {
        // Platz für Werkzeuge (A−, A+, Anmerkungen, Öffnen, Anlage) und „n weitere“
        const frei = leiste.clientWidth - 260
        this.sichtbareReiter = Math.max(1, Math.floor(frei / REITER_BREITE))
      })
      reiterBeobachter.observe(leiste)
    },

    // ---------- Arbeitsspalte ----------
    reiterWaehlen(reiter: Reiter): void {
      this.reiter = reiter
      merken(SPEICHER_REITER, reiter)
      if (reiter === 'rede') void this.syncSpeechEditor()
    },

    /** Pfeiltasten in der Reiterleiste (Muster „Tabs“ der WAI-ARIA-Praxis) */
    reiterTaste(e: KeyboardEvent): void {
      const i = REITER.indexOf(this.reiter)
      let neu = i
      if (e.key === 'ArrowRight') neu = (i + 1) % REITER.length
      else if (e.key === 'ArrowLeft') neu = (i - 1 + REITER.length) % REITER.length
      else if (e.key === 'Home') neu = 0
      else if (e.key === 'End') neu = REITER.length - 1
      else return
      e.preventDefault()
      e.stopPropagation()
      this.reiterWaehlen(REITER[neu])
      void this.$nextTick(() => document.getElementById(`reiter-${REITER[neu]}`)?.focus())
    },

    // ---------- Position ----------
    setzeAndere(code: string): void {
      if (code) this.setPosition(code)
    },

    endgueltigSetzen(): void {
      const item = this.selectedItem
      if (item) void this.savePositionFields(item, { is_final: item.isFinal })
    },

    positionsKlasse(code: string | undefined): string {
      return positionsKlasse(code)
    },

    /** Zeile „Im Beratungsverlauf“: Ergebnis, endgültig und Begründung als Text */
    verlaufZusatz(cp: Record<string, unknown>): string {
      const teile: string[] = []
      if (cp.outcome_display) teile.push(`Ergebnis: ${String(cp.outcome_display).toLowerCase()}`)
      if (cp.is_final) teile.push('endgültig')
      let text = teile.length ? `, ${teile.join(', ')}.` : '.'
      if (cp.reasoning) text += ` „${String(cp.reasoning)}“`
      return text
    },

    // ---------- Blätter und Menüs ----------
    oeffneBlatt(art: Blatt): void {
      this.blatt = art
      if (art === 'unterlagen') {
        this._gezeigt = ''
        this.zeigeUnterlage()
      }
    },

    schliesseBlatt(): void {
      this.blatt = ''
    },

    sitzungsnotizenOeffnen(): void {
      this.menueOffen = false
      this.sitzungsnotizenOffen = true
    },

    zusammenfassungOeffnen(): void {
      this.menueOffen = false
      void this.openSummary()
    },

    legendeOeffnen(): void {
      this.menueOffen = false
      this.showLegend = true
    },
  })
}
