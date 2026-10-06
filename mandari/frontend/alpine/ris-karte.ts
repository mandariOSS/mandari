/**
 * Karte der Recherche in Work (templates/work/ris/partials/_karte.html, Issue #853), vorher Inline-Skript.
 *
 * Markup: `<div x-data="risKarte" data-kacheln="…/tiles/0/0/0" data-daten="…/ris/map/data/"
 * data-vorgang="…/ris/papers/<null-uuid>/" data-zeitraum="12">` mit der Kartenfläche `[data-karte]` und
 * `#ris-karte-config` (json_script: Zentrum und Rahmen der Kommune).
 *
 * - Kacheln kommen über den Kachel-Proxy von mandari, nie direkt von einem Kartendienst.
 * - Punkte lädt die Karte je Ausschnitt und Zeitraum nach (nicht mehr nur die 500 neuesten Vorgänge). Liegen im
 *   Ausschnitt mehr Punkte, als eine Antwort trägt, sagt der Status das und bittet ums Hineinzoomen.
 * - Titel und Ortsnamen gelangen nur als Text in die Popups (`textContent`), nie als HTML.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'
import { placeStyle } from '../js/paper-map'

type LatLng = [number, number]

interface Rahmen {
  north: number | null
  south: number | null
  east: number | null
  west: number | null
}

interface KarteConfig {
  center_lat: number
  center_lng: number
  zoom: number
  bbox: Rahmen | null
}

export interface KarteFeature {
  geometry: { coordinates: [number, number] }
  properties: { id: string; title: string; reference: string | null; date: string | null; location_name: string }
}

export interface KarteAntwort {
  features: KarteFeature[]
  truncated: boolean
}

interface Ausschnitt {
  toBBoxString(): string
}

interface Ebene {
  addTo(map: KarteMap): Ebene
  bindPopup(content: HTMLElement, options?: Record<string, unknown>): Ebene
}

interface Gruppe extends Ebene {
  clearLayers(): Gruppe
  addLayer(layer: Ebene): Gruppe
}

interface KarteMap {
  setView(center: LatLng, zoom: number): KarteMap
  fitBounds(bounds: unknown, options?: Record<string, unknown>): KarteMap
  getBounds(): Ausschnitt
  on(event: string, handler: () => void): KarteMap
}

interface LeafletApi {
  map(element: HTMLElement, options: Record<string, unknown>): KarteMap
  tileLayer(url: string, options: Record<string, unknown>): Ebene
  circleMarker(latlng: LatLng, options: Record<string, unknown>): Ebene
  layerGroup(): Gruppe
  markerClusterGroup?: (options: Record<string, unknown>) => Gruppe
  latLngBounds(southWest: LatLng, northEast: LatLng): unknown
}

/** Platzhalter in der Adresse des Vorgangs (`data-vorgang`), wird je Punkt ersetzt */
const NULL_UUID = '00000000-0000-0000-0000-000000000000'
const NAMEN: Record<string, string> = { '3': '3 Monate', '12': '12 Monate', '36': '3 Jahre', alle: 'alle Jahre' }
const zahl = new Intl.NumberFormat('de-DE')

function leaflet(): LeafletApi | undefined {
  return (window as unknown as { L?: LeafletApi }).L
}

/** „12.03.2026“ aus einem ISO-Datum, sonst leer */
export function datumText(iso: string | null): string {
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
  const wurzel = document.createElement('div')
  wurzel.className = 'ris-karte-popup'
  const link = document.createElement('a')
  link.href = vorgangUrl.replace(NULL_UUID, encodeURIComponent(p.id))
  link.textContent = p.title || 'Vorgang'
  wurzel.append(link)
  const angaben = [p.reference ?? '', datumText(p.date)].filter(Boolean).join(' · ')
  for (const text of [angaben, p.location_name]) {
    if (!text) continue
    const zeile = document.createElement('p')
    zeile.textContent = text
    wurzel.append(zeile)
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
  _abbruch: undefined as AbortController | undefined,
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
    const map = L.map(flaeche, { zoomControl: true, attributionControl: true })
    const rahmen = config?.bbox
    if (rahmen?.north != null && rahmen.south != null && rahmen.east != null && rahmen.west != null) {
      map.fitBounds(L.latLngBounds([rahmen.south, rahmen.west], [rahmen.north, rahmen.east]), { padding: [24, 24] })
    } else {
      map.setView([config?.center_lat ?? 51.1657, config?.center_lng ?? 10.4515], config ? config.zoom : 6)
    }
    L.tileLayer((root.dataset.kacheln ?? '').replace('/0/0/0', '/{z}/{x}/{y}'), {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    }).addTo(map)
    const gruppe = L.markerClusterGroup
      ? L.markerClusterGroup({ maxClusterRadius: 45, showCoverageOnHover: false, spiderfyOnMaxZoom: true })
      : L.layerGroup()
    gruppe.addTo(map)
    this._map = map
    this._punkte = gruppe
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
    if (!map || !L || !this._punkte) return
    this._abbruch?.abort()
    const abbruch = new AbortController()
    this._abbruch = abbruch
    this.laedt = true
    const params = new URLSearchParams({ bbox: map.getBounds().toBBoxString(), zeitraum: this.zeitraum })
    try {
      const antwort = await fetch(`${this._datenUrl}?${params}`, {
        signal: abbruch.signal,
        headers: { Accept: 'application/json' },
      })
      if (!antwort.ok) throw new Error(String(antwort.status))
      const daten = (await antwort.json()) as KarteAntwort
      this._punkte.clearLayers()
      const stil = placeStyle()
      for (const feature of daten.features) {
        const [lon, lat] = feature.geometry.coordinates
        const punkt = L.circleMarker([lat, lon], stil)
        punkt.bindPopup(popupInhalt(feature, this._vorgangUrl), { maxWidth: 300 })
        this._punkte.addLayer(punkt)
      }
      this.status = statusText(daten, this.zeitraum)
      this.laedt = false
    } catch (fehler) {
      if ((fehler as Error).name === 'AbortError') return
      this.status = 'Die Vorgänge konnten nicht geladen werden. Bitte versuchen Sie es erneut.'
      this.laedt = false
    }
  },
}))
