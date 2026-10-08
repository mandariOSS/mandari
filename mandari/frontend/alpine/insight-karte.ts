/**
 * Karte im Bürgerportal (templates/pages/map.html, Issue #853), vorher Inline-Skript `mapApp()` in der Seite.
 *
 * Markup: `<div x-data="insightKarte" data-kacheln="…/tiles/0/0/0" data-daten="…/karte/partials/markers/">` mit der
 * Kartenfläche `#map` und `#insight-karte-start` (json_script: Rahmen bzw. Zentrum der Kommune).
 *
 * Karte, Kacheln über den Kachel-Proxy, Punktgruppe, Abfrage und Zeichnen kommen aus dem gemeinsamen Kartenmodul mit
 * Work (frontend/js/vorgangskarte.ts); Aussehen und Verhalten bleiben wie bisher: Zeitraum in Wochen, Zähler der
 * Orte, Punkte als Marker mit Popup („Details ansehen“), Einpassen auf die Punkte, und erst wenn der Server die
 * Liste kappt (`truncated`), lädt die Karte beim Verschieben je Ausschnitt nach. Neu: Titel und Ortsnamen gelangen
 * nur als Text ins Popup (vorher als HTML).
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'
import {
  ausschnitt,
  type Gruppe,
  type KarteFeature,
  type KarteMap,
  type KartenStart,
  karteAnlegen,
  knoten,
  type LatLng,
  leaflet,
  PunkteAbfrage,
  punkteZeichnen,
  punktGruppe,
  type Rahmen,
} from '../js/vorgangskarte'

interface Start {
  rahmen: Rahmen | null
  zentrum: LatLng | null
}

const SVG = 'http://www.w3.org/2000/svg'
/** Ortsmarke und Pfeil wie bisher im Popup (Heroicons, Strichstärke 2) */
const ORT = [
  'M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  'M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1 1 15 0Z',
]
const PFEIL = ['m8.25 4.5 7.5 7.5-7.5 7.5']

function symbol(klassen: string, pfade: string[]): SVGSVGElement {
  const svg = document.createElementNS(SVG, 'svg')
  svg.setAttribute('class', klassen)
  svg.setAttribute('fill', 'none')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke', 'currentColor')
  for (const d of pfade) {
    const pfad = document.createElementNS(SVG, 'path')
    pfad.setAttribute('stroke-linecap', 'round')
    pfad.setAttribute('stroke-linejoin', 'round')
    pfad.setAttribute('d', d)
    svg.append(pfad)
  }
  return svg
}

/** Popup eines Punkts wie bisher: Titel, Vorlagen-Nr., Ort und „Details ansehen“; Texte nur als Text */
export function insightPopup(feature: KarteFeature): HTMLElement {
  const p = feature.properties
  const wurzel = knoten('div', 'p-3')
  wurzel.append(
    knoten('p', 'font-medium text-gray-900 dark:text-white text-sm leading-snug mb-1', p.title || 'Vorgang'),
  )
  if (p.reference) wurzel.append(knoten('p', 'text-xs text-gray-500 font-mono mb-1.5', p.reference))
  if (p.location_name) {
    const ort = knoten('p', 'text-xs text-gray-400 mb-2 flex items-center gap-1')
    ort.append(symbol('w-3 h-3 shrink-0', ORT), ` ${p.location_name}`)
    wurzel.append(ort)
  }
  const link = knoten(
    'a',
    'inline-flex items-center gap-1 text-xs text-primary-600 hover:text-primary-700 dark:text-primary-400 font-medium',
    'Details ansehen',
  )
  link.href = p.url ?? ''
  link.append(symbol('w-3 h-3', PFEIL))
  wurzel.append(link)
  return wurzel
}

export const insightKarte = defineComponent(() => ({
  timeRange: '12',
  markerCount: 0,
  loading: true,
  timeOptions: [
    { value: '4', label: '4 Wo.' },
    { value: '12', label: '3 Mo.' },
    { value: '26', label: '6 Mo.' },
    { value: '52', label: '1 Jahr' },
    { value: 'all', label: 'Alle' },
  ],
  _datenUrl: '',
  // Leaflet-Objekte nicht reaktiv halten (Alpine würde sie in Proxys hüllen)
  _map: undefined as KarteMap | undefined,
  _markers: undefined as Gruppe | undefined,
  _markerIcon: undefined as unknown,
  _abfrage: new PunkteAbfrage(),
  _bboxMode: false,
  _refetchTimer: 0,

  init() {
    const root = this.$root as HTMLElement
    this._datenUrl = root.dataset.daten ?? ''
    const L = leaflet()
    const flaeche = root.querySelector<HTMLElement>('#map')
    if (!L || !flaeche || !this._datenUrl) {
      this.loading = false
      return
    }
    const start = readJsonScript<Start>('insight-karte-start')
    const optionen: KartenStart = {
      rahmen: start?.rahmen,
      zentrum: start?.zentrum,
      zoom: 12,
      rand: 30,
      hoechsterZoom: 13,
    }
    const map = karteAnlegen(L, flaeche, root.dataset.kacheln ?? '', optionen)
    this._map = map
    this._markers = punktGruppe(L, map, 50)
    this._markerIcon = L.divIcon({
      className: 'custom-marker',
      html: '<div class="marker-dot"></div>',
      iconSize: [22, 22],
      iconAnchor: [11, 11],
      popupAnchor: [0, -11],
    })
    // Ausschnitt-Modus: Kappt der Server die Liste (truncated), lädt die Karte beim Verschieben je Ausschnitt nach
    map.on('moveend', () => {
      if (!this._bboxMode || this.loading) return
      window.clearTimeout(this._refetchTimer)
      this._refetchTimer = window.setTimeout(() => void this.loadMarkers(true), 300)
    })
    void this.loadMarkers()
  },

  setTimeRange(value: string) {
    if (this.timeRange === value) return
    this.timeRange = value
    this._bboxMode = false
    void this.loadMarkers()
  },

  async loadMarkers(useBbox = false) {
    const map = this._map
    const L = leaflet()
    const gruppe = this._markers
    if (!map || !L || !gruppe) return
    this.loading = true
    const params = new URLSearchParams(this.timeRange === 'all' ? { all: '1' } : { weeks: this.timeRange })
    if (useBbox) params.set('bbox', ausschnitt(map))
    try {
      const daten = await this._abfrage.laden(this._datenUrl, params)
      if (!daten) return
      if (daten.truncated) this._bboxMode = true
      const icon = this._markerIcon
      punkteZeichnen(gruppe, daten.features, (lage) => L.marker(lage, { icon }), insightPopup, { maxWidth: 280 })
      this.markerCount = daten.features.length
      this.loading = false
      // Auf die Punkte einpassen (nicht beim Nachladen je Ausschnitt – sonst Endlosschleife)
      if (!useBbox && gruppe.getLayers().length > 0) {
        map.fitBounds(gruppe.getBounds(), { padding: [40, 40], maxZoom: 14 })
      }
    } catch (fehler) {
      console.error('Map markers error:', fehler)
      this.loading = false
    }
  },
}))
