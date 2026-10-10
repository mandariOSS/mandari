/**
 * Karte der Recherche in Work (templates/work/ris/partials/_karte.html, Issue #853), vorher Inline-Skript.
 *
 * Markup: `<div x-data="risKarte" data-kacheln="…/tiles/0/0/0" data-daten="…/ris/map/data/"
 * data-vorgang="…/ris/papers/<null-uuid>/" data-zeitraum="12">` mit der Kartenfläche `[data-karte]` und
 * `#ris-karte-config` (json_script: Zentrum und Rahmen der Kommune).
 *
 * Karte, Kacheln über den Kachel-Proxy von mandari, Punktgruppe, Abfrage je Ausschnitt und Zeichnen kommen aus dem
 * gemeinsamen Kartenmodul mit Insight (frontend/js/vorgangskarte.ts). Hier stehen nur die Teile von Work: Zeitraum
 * in Monaten (in der Adresse), Status als Satz unter der Karte, Popup mit Link zum Vorgang in Work.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'
import { placeStyle } from '../js/paper-map'
import {
  ausschnitt,
  type Gruppe,
  type KarteAntwort,
  type KarteFeature,
  type KarteMap,
  karteAnlegen,
  knoten,
  leaflet,
  PunkteAbfrage,
  punkteZeichnen,
  punktGruppe,
  type Rahmen,
} from '../js/vorgangskarte'

export type { KarteAntwort, KarteFeature } from '../js/vorgangskarte'

interface KarteConfig {
  center_lat: number
  center_lng: number
  zoom: number
  bbox: Rahmen | null
}

/** Platzhalter in der Adresse des Vorgangs (`data-vorgang`), wird je Punkt ersetzt */
const NULL_UUID = '00000000-0000-0000-0000-000000000000'
const NAMEN: Record<string, string> = { '3': '3 Monate', '12': '12 Monate', '36': '3 Jahre', alle: 'alle Jahre' }
const zahl = new Intl.NumberFormat('de-DE')

/** „12.03.2026“ aus einem ISO-Datum, sonst leer */
export function datumText(iso: string | null | undefined): string {
  if (!iso) return ''
  const [jahr, monat, tag] = iso.split('-')
  return jahr && monat && tag ? `${tag.slice(0, 2)}.${monat}.${jahr}` : ''
}

/** Status unter der Karte: wie viele Vorgänge an wie vielen Orten, oder der Hinweis zum Hineinzoomen */
export function statusText(antwort: KarteAntwort, zeitraum: string): string {
  const orte = antwort.features.length
  const vorgaenge = new Set(antwort.features.map((f) => f.properties.id)).size
  const spanne = zeitraum === 'alle' ? 'aus allen Jahren' : `aus den letzten ${NAMEN[zeitraum] ?? '12 Monate'}n`
  if (antwort.truncated) {
    return `In diesem Ausschnitt liegen mehr als ${zahl.format(orte)} Orte; eingezeichnet sind die neuesten Vorgänge. Zoomen Sie hinein, um alle zu sehen.`
  }
  if (!orte) return `In diesem Ausschnitt gibt es keine verorteten Vorgänge ${spanne}.`
  const v = vorgaenge === 1 ? '1 Vorgang' : `${zahl.format(vorgaenge)} Vorgänge`
  const o = orte === 1 ? '1 Ort' : `${zahl.format(orte)} Orten`
  return `In diesem Ausschnitt: ${v} an ${o}, ${spanne}.`
}

/** Popup eines Punkts aus DOM-Knoten: Titel als Link zum Vorgang in Work, darunter Nummer, Datum und Ort */
export function popupInhalt(feature: KarteFeature, vorgangUrl: string): HTMLElement {
  const p = feature.properties
  const wurzel = knoten('div', 'ris-karte-popup')
  const link = knoten('a', '', p.title || 'Vorgang')
  link.href = vorgangUrl.replace(NULL_UUID, encodeURIComponent(p.id))
  wurzel.append(link)
  const angaben = [p.reference ?? '', datumText(p.date)].filter(Boolean).join(' · ')
  for (const text of [angaben, p.location_name]) {
    if (text) wurzel.append(knoten('p', '', text))
  }
  return wurzel
}

export const risKarte = defineComponent(() => ({
  zeitraum: '12',
  status: 'Die Karte wird geladen …',
  laedt: true,
  _datenUrl: '',
  _vorgangUrl: '',
  // Leaflet-Objekte nicht reaktiv halten (Alpine würde sie in Proxys hüllen)
  _map: undefined as KarteMap | undefined,
  _punkte: undefined as Gruppe | undefined,
  _abfrage: new PunkteAbfrage(),
  _timer: 0,

  init() {
    const root = this.$root as HTMLElement
    this._datenUrl = root.dataset.daten ?? ''
    this._vorgangUrl = root.dataset.vorgang ?? ''
    this.zeitraum = root.dataset.zeitraum || '12'
    const L = leaflet()
    const flaeche = root.querySelector<HTMLElement>('[data-karte]')
    if (!L || !flaeche || !this._datenUrl) {
      this.status = 'Die Karte konnte nicht geladen werden.'
      this.laedt = false
      return
    }
    const config = readJsonScript<KarteConfig>('ris-karte-config')
    const map = karteAnlegen(L, flaeche, root.dataset.kacheln ?? '', {
      rahmen: config?.bbox,
      zentrum: config ? [config.center_lat, config.center_lng] : null,
      zoom: config?.zoom,
      rand: 24,
    })
    this._map = map
    this._punkte = punktGruppe(L, map, 45)
    map.on('moveend', () => {
      window.clearTimeout(this._timer)
      this._timer = window.setTimeout(() => void this.laden(), 250)
    })
    void this.laden()
  },

  zeitraumWaehlen(wert: string) {
    if (wert === this.zeitraum) return
    this.zeitraum = wert
    const adresse = new URL(window.location.href)
    adresse.searchParams.set('zeitraum', wert)
    window.history.replaceState(null, '', adresse)
    void this.laden()
  },

  async laden() {
    const map = this._map
    const L = leaflet()
    const gruppe = this._punkte
    if (!map || !L || !gruppe) return
    this.laedt = true
    try {
      const params = new URLSearchParams({ bbox: ausschnitt(map), zeitraum: this.zeitraum })
      const daten = await this._abfrage.laden(this._datenUrl, params)
      if (!daten) return
      const stil = placeStyle()
      punkteZeichnen(
        gruppe,
        daten.features,
        (lage) => L.circleMarker(lage, stil),
        (feature) => popupInhalt(feature, this._vorgangUrl),
        { maxWidth: 300 },
      )
      this.status = statusText(daten, this.zeitraum)
      this.laedt = false
    } catch {
      this.status = 'Die Vorgänge konnten nicht geladen werden. Bitte versuchen Sie es erneut.'
      this.laedt = false
    }
  },
}))
