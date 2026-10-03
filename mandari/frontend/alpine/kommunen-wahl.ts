/**
 * Kommunenwechsel des Bürgerportals (Issue #783, Stufe 2) – für Tausende Kommunen, nie als lange Liste.
 *
 * - Vorschläge während der Eingabe (Name, Ortsteil oder Postleitzahl), höchstens acht, vom Server
 *   (`data-vorschlaege-url`).
 * - „In meiner Nähe“ nur auf Klick: Der Browser fragt den Standort, schickt nur eine Zelle von 0,1 Grad
 *   (`data-naehe-url`) und sortiert die Kandidaten selbst nach der genauen Entfernung. Gespeichert wird nichts.
 * - „Zuletzt besucht“ lokal im Browser (localStorage, kein Cookie), höchstens fünf.
 * - Stöbern Land → Kreis → (Gemeindeverband →) Kommune in Stufen (`data-stoebern-url`).
 *
 * Markup: `templates/components/kommunen_wahl.html` (im Dialog des Rahmens und auf der Auswahlseite). Ergebnisse sind
 * Links; Pfeiltasten wandern zwischen Eingabe und Links, Escape kehrt zur Eingabe zurück. Kommunen ohne Daten
 * erscheinen als Text mit „noch nicht verfügbar“.
 */

import { defineComponent } from '../js/alpine/component'

export interface KommuneTreffer {
  name: string
  ort: string
  hinweis?: string
  url?: string
  verfuegbar: boolean
  breite?: number
  laenge?: number
  entfernung?: number
}

export interface StoebernEintrag extends Partial<KommuneTreffer> {
  art: 'gruppe' | 'kommune'
  name: string
  land?: string
  kreis?: string
  verband?: string
  anzahl?: number
  mit_daten?: number
}

export interface StoebernStufe {
  stufe: string
  titel: string
  zurueck: { titel: string; land?: string; kreis?: string } | null
  eintraege: StoebernEintrag[]
}

interface NaeheAntwort {
  kandidaten: KommuneTreffer[]
  naechste_mit_daten: KommuneTreffer | null
}

export const ZULETZT_SCHLUESSEL = 'mandari.insight.zuletzt'
export const MAX_ZULETZT = 5
const MAX_NAEHE = 8
const VERZOEGERUNG_MS = 150

/** Zuletzt besuchte Kommunen aus dem Browser; fehlerhafte Einträge fallen weg. */
export function zuletztLesen(): KommuneTreffer[] {
  try {
    const daten = JSON.parse(localStorage.getItem(ZULETZT_SCHLUESSEL) ?? '[]') as unknown
    if (!Array.isArray(daten)) return []
    return daten
      .filter(
        (e): e is KommuneTreffer => typeof e?.name === 'string' && typeof e?.url === 'string' && e.url.startsWith('/'),
      )
      .slice(0, MAX_ZULETZT)
      .map((e) => ({ name: e.name, ort: typeof e.ort === 'string' ? e.ort : '', url: e.url, verfuegbar: true }))
  } catch {
    return []
  }
}

/** Kommune vorn in „Zuletzt besucht“ ablegen (nur Name, Ort und Adresse; ohne Speicher nur für diese Seite). */
export function zuletztMerken(kommune: { name: string; ort?: string; url?: string }): void {
  if (!kommune.url?.startsWith('/') || !kommune.name) return
  const neu = { name: kommune.name, ort: kommune.ort ?? '', url: kommune.url }
  const liste = [neu, ...zuletztLesen().filter((e) => e.url !== neu.url)].slice(0, MAX_ZULETZT)
  try {
    localStorage.setItem(ZULETZT_SCHLUESSEL, JSON.stringify(liste))
  } catch {
    // Speicher gesperrt (privater Modus): dann eben ohne „Zuletzt besucht“
  }
}

/** Zelle von 0,1 Grad, wie sie an den Server geht (abgerundet, eine Nachkommastelle). */
export function zelleVon(breite: number, laenge: number): string {
  const runden = (wert: number): string => (Math.floor(wert * 10) / 10).toFixed(1)
  return `${runden(breite)},${runden(laenge)}`
}

/** Entfernung in Kilometern (Haversine). */
export function entfernungKm(a: [number, number], b: [number, number]): number {
  const rad = (grad: number): number => (grad * Math.PI) / 180
  const h =
    Math.sin(rad(b[0] - a[0]) / 2) ** 2 +
    Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(rad(b[1] - a[1]) / 2) ** 2
  return 2 * 6371 * Math.asin(Math.sqrt(h))
}

/** Kandidaten nach Entfernung sortiert, die nächsten `anzahl` mit gerundeter Entfernung. */
export function naechste(
  kandidaten: KommuneTreffer[],
  standort: [number, number],
  anzahl = MAX_NAEHE,
): KommuneTreffer[] {
  return kandidaten
    .filter((k) => typeof k.breite === 'number' && typeof k.laenge === 'number')
    .map((k) => ({ ...k, entfernung: entfernungKm(standort, [k.breite as number, k.laenge as number]) }))
    .sort((a, b) => (a.entfernung ?? 0) - (b.entfernung ?? 0))
    .slice(0, anzahl)
}

export function entfernungText(km: number | undefined): string {
  if (km === undefined) return ''
  return km < 1 ? 'unter 1 km' : `${Math.round(km).toLocaleString('de-DE')} km`
}

export const kommunenWahl = defineComponent(() => ({
  eingabe: '',
  treffer: [] as KommuneTreffer[],
  sucht: false,
  meldung: '',
  zuletzt: [] as KommuneTreffer[],
  naehe: null as KommuneTreffer[] | null,
  naechsteMitDaten: null as KommuneTreffer | null,
  standort: '' as '' | 'laedt' | 'fehler' | 'verweigert' | 'leer',
  stufe: null as StoebernStufe | null,
  _adressen: {} as Record<string, string>,
  _abbrueche: {} as Record<string, AbortController>,
  _zeitgeber: undefined as ReturnType<typeof setTimeout> | undefined,

  init() {
    const daten = (this.$el as HTMLElement).dataset
    this._adressen = {
      vorschlaege: daten.vorschlaegeUrl ?? '',
      naehe: daten.naeheUrl ?? '',
      stoebern: daten.stoebernUrl ?? '',
    }
    this.zuletzt = zuletztLesen()
    this.$watch('eingabe', () => {
      clearTimeout(this._zeitgeber)
      this._zeitgeber = setTimeout(() => this.suchen(), VERZOEGERUNG_MS)
    })
    // Im Dialog des Rahmens: beim Öffnen „Zuletzt besucht“ auffrischen und die erste Stufe des Stöberns laden
    const offen = (): boolean => Boolean((this as unknown as { cityModalOpen?: boolean }).cityModalOpen)
    if (daten.imDialog !== undefined) {
      this.$watch('cityModalOpen', (wert: boolean) => {
        if (!wert) return
        this.zuletzt = zuletztLesen()
        if (!this.stufe) this.stoebern()
        // Nach der Fokusfalle (sie setzt den Fokus auf das erste Element) ins Suchfeld
        setTimeout(() => (this.$refs.eingabe as HTMLInputElement | undefined)?.focus(), 50)
      })
      if (offen()) this.stoebern()
    } else {
      this.stoebern()
    }
  },

  /** JSON laden; eine neue Anfrage derselben Art bricht die vorige ab (schnelles Tippen). */
  async laden<T>(art: string, adresse: string): Promise<T | null> {
    this._abbrueche[art]?.abort()
    const abbruch = new AbortController()
    this._abbrueche[art] = abbruch
    try {
      const antwort = await fetch(adresse, { signal: abbruch.signal, headers: { Accept: 'application/json' } })
      if (!antwort.ok) return null
      return (await antwort.json()) as T
    } catch {
      return null
    }
  },

  async suchen() {
    const text = this.eingabe.trim()
    if (text.length < 2) {
      this.treffer = []
      this.meldung = ''
      return
    }
    this.sucht = true
    const daten = await this.laden<{ treffer: KommuneTreffer[] }>(
      'vorschlaege',
      `${this._adressen.vorschlaege}?q=${encodeURIComponent(text)}`,
    )
    if (this.eingabe.trim() !== text) return // inzwischen weitergetippt
    this.sucht = false
    this.treffer = daten?.treffer ?? []
    const anzahl = this.treffer.length
    this.meldung =
      daten === null
        ? 'Die Suche ist gerade nicht erreichbar.'
        : anzahl
          ? `${anzahl} ${anzahl === 1 ? 'Vorschlag' : 'Vorschläge'}`
          : 'Keine Kommune gefunden.'
  },

  /** Standort nur auf Klick; an den Server geht nur die Zelle, die Position wird nicht gespeichert. */
  inDerNaehe() {
    if (!('geolocation' in navigator)) {
      this.standort = 'fehler'
      return
    }
    this.standort = 'laedt'
    this.meldung = 'Standort wird ermittelt …'
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const punkt: [number, number] = [position.coords.latitude, position.coords.longitude]
        const zelle = zelleVon(punkt[0], punkt[1])
        const daten = await this.laden<NaeheAntwort>('naehe', `${this._adressen.naehe}?zelle=${zelle}`)
        if (daten === null) {
          this.standort = 'fehler'
          this.meldung = 'Kommunen in der Nähe sind gerade nicht erreichbar.'
          return
        }
        this.naehe = naechste(daten.kandidaten, punkt)
        const mitDaten = daten.naechste_mit_daten
        this.naechsteMitDaten =
          mitDaten && typeof mitDaten.breite === 'number' && typeof mitDaten.laenge === 'number'
            ? { ...mitDaten, entfernung: entfernungKm(punkt, [mitDaten.breite, mitDaten.laenge]) }
            : mitDaten
        this.standort = this.naehe.length ? '' : 'leer'
        this.meldung = this.naehe.length
          ? `${this.naehe.length} Kommunen in Ihrer Nähe`
          : 'Keine Kommune in der Nähe gefunden.'
      },
      (fehler) => {
        this.standort = fehler.code === fehler.PERMISSION_DENIED ? 'verweigert' : 'fehler'
        this.meldung =
          this.standort === 'verweigert'
            ? 'Der Standort wurde nicht freigegeben. Suchen Sie nach Name oder Postleitzahl.'
            : 'Der Standort ließ sich nicht ermitteln.'
      },
      { enableHighAccuracy: false, timeout: 10_000, maximumAge: 600_000 },
    )
  },

  async stoebern(parameter: { land?: string; kreis?: string; verband?: string } = {}) {
    const abfrage = new URLSearchParams()
    for (const [schluessel, wert] of Object.entries(parameter)) if (wert) abfrage.set(schluessel, wert)
    const daten = await this.laden<StoebernStufe>('stoebern', `${this._adressen.stoebern}?${abfrage.toString()}`)
    if (daten !== null) {
      const vorher = this.stufe
      this.stufe = daten
      // Nach einem Wechsel der Stufe den Fokus an den Titel geben, damit Bildschirmleser die neue Stufe ansagen
      if (vorher) this.$nextTick(() => (this.$refs.stufenTitel as HTMLElement | undefined)?.focus())
    }
  },

  sucheAktiv(): boolean {
    return this.eingabe.trim().length >= 2
  },

  /** Was unter dem Suchfeld steht: Vorschläge, sonst die Nähe, sonst „Zuletzt besucht“. */
  liste(): { titel: string; eintraege: KommuneTreffer[] } {
    if (this.sucheAktiv()) return { titel: '', eintraege: this.treffer }
    if (this.naehe) return { titel: 'In Ihrer Nähe', eintraege: this.naehe }
    return { titel: this.zuletzt.length ? 'Zuletzt besucht' : '', eintraege: this.zuletzt }
  },

  /** Zweite Zeile eines Eintrags: Treffer auf Ortsteil oder Postleitzahl, dann Kreis und Land. */
  zweiteZeile(eintrag: KommuneTreffer): string {
    return [eintrag.hinweis, eintrag.ort].filter(Boolean).join(', ')
  },

  stoebernInfo(eintrag: StoebernEintrag): string {
    if (eintrag.art !== 'gruppe') return eintrag.ort ?? ''
    const anzahl = eintrag.anzahl ?? 0
    const teile = [eintrag.ort, `${anzahl.toLocaleString('de-DE')} ${anzahl === 1 ? 'Kommune' : 'Kommunen'}`]
    if (eintrag.mit_daten) teile.push(`${eintrag.mit_daten} mit Daten`)
    return teile.filter(Boolean).join(', ')
  },

  stufenTitel(): string {
    if (!this.stufe || this.stufe.stufe === 'land') return 'Nach Bundesland suchen'
    return this.stufe.titel
  },

  merken(kommune: KommuneTreffer) {
    zuletztMerken(kommune)
  },

  /** Pfeiltasten zwischen Eingabe und Ergebnislinks; Escape zurück zur Eingabe. */
  tasten(event: KeyboardEvent) {
    const links = Array.from((this.$root as HTMLElement).querySelectorAll<HTMLElement>('[data-kommune-ziel]')).filter(
      (el) => el.offsetParent !== null,
    )
    const eingabe = this.$refs.eingabe as HTMLInputElement | undefined
    const index = links.indexOf(document.activeElement as HTMLElement)
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      links[Math.min(index + 1, links.length - 1)]?.focus()
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (index <= 0) eingabe?.focus()
      else links[index - 1]?.focus()
    } else if (event.key === 'Escape' && document.activeElement !== eingabe && index >= 0) {
      event.stopPropagation()
      eingabe?.focus()
    }
  },

  entfernungText,
}))
