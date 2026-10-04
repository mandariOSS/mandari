/**
 * Nachbarschaftssuche des Bürgerportals (templates/pages/neighborhood.html), vorher Inline-Skript.
 *
 * Markup: `<div x-data="neighborhoodApp" data-center-lat data-center-lon data-tile-url
 * data-autocomplete-url data-results-url>` mit `#neighborhood-map` und `#results-container`.
 * Stadtteile übergeben Name und Koordinaten als Datenattribute (`data-name`, `data-lat`, `data-lon`),
 * nie als Quelltext. Titel aus den Ergebnissen gelangen nur als Text in die Popups.
 */

import { defineComponent } from '../js/alpine/component'
import { PLACE_STYLE, popupContent } from '../js/paper-map'

type LatLng = [number, number]

export interface PlaceSuggestion {
  name: string
  lat: number
  lon: number
}

interface Layer {
  addTo(map: NeighborhoodMap): Layer
  bindPopup(content: HTMLElement): Layer
  getBounds(): unknown
}

interface NeighborhoodMap {
  removeLayer(layer: Layer): NeighborhoodMap
  fitBounds(bounds: unknown, options?: Record<string, unknown>): NeighborhoodMap
}

interface LeafletApi {
  map(element: HTMLElement, options: Record<string, unknown>): NeighborhoodMap
  tileLayer(url: string, options: Record<string, unknown>): Layer
  circle(latlng: LatLng, options: Record<string, unknown>): Layer
  circleMarker(latlng: LatLng, options: Record<string, unknown>): Layer
}

/** Ersatzmitte, wenn die Kommune keine Koordinaten hat */
const FALLBACK_CENTER: LatLng = [51.1657, 10.4515]
const CENTER_STYLE = { radius: 9, color: '#ffffff', weight: 3, fillColor: '#dc2626', fillOpacity: 1 }

function leaflet(): LeafletApi | undefined {
  return (window as unknown as { L?: LeafletApi }).L
}

function number(value: string | undefined): number | null {
  const parsed = Number.parseFloat(value ?? '')
  return Number.isFinite(parsed) ? parsed : null
}

/** Umkreis lesbar: 250 m, 1 km, 2 km */
export function radiusLabel(radius: number): string {
  return radius >= 1000 ? `${radius / 1000} km` : `${radius} m`
}

export const neighborhoodApp = defineComponent(() => ({
  searchQuery: '',
  suggestions: [] as PlaceSuggestion[],
  selectedLat: null as number | null,
  selectedLon: null as number | null,
  selectedName: '' as string,
  radius: 500,
  loading: false,
  // Leaflet-Objekte nicht reaktiv halten (Alpine würde sie in Proxys hüllen)
  _map: undefined as NeighborhoodMap | undefined,
  _areaLayers: [] as Layer[],
  _resultLayers: [] as Layer[],

  init() {
    this.initMap()
    const params = new URLSearchParams(window.location.search)
    const lat = number(params.get('lat') ?? undefined)
    const lon = number(params.get('lon') ?? undefined)
    if (lat !== null && lon !== null) {
      this.selectedLat = lat
      this.selectedLon = lon
      this.selectedName = params.get('name') || ''
      this.searchQuery = this.selectedName
      const radius = Number.parseInt(params.get('radius') ?? '', 10)
      if (Number.isFinite(radius) && radius > 0) this.radius = radius
      this.$nextTick(() => {
        this.updateMap()
        void this.fetchResults()
      })
    }
  },

  radiusLabel,

  initMap() {
    const L = leaflet()
    const element = document.getElementById('neighborhood-map')
    if (!L || !element) return
    const root = this.$el as HTMLElement
    const lat = number(root.dataset.centerLat)
    const lon = number(root.dataset.centerLon)
    const center: LatLng = lat !== null && lon !== null ? [lat, lon] : FALLBACK_CENTER
    const map = L.map(element, { center, zoom: lat !== null ? 14 : 6, zoomControl: true, attributionControl: true })
    const tileUrl = (root.dataset.tileUrl ?? '').replace('/0/0/0', '/{z}/{x}/{y}')
    L.tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    }).addTo(map)
    this._map = map
  },

  async fetchSuggestions() {
    if (this.searchQuery.length < 2) {
      this.suggestions = []
      return
    }
    const url = `${(this.$el as HTMLElement).dataset.autocompleteUrl ?? ''}?q=${encodeURIComponent(this.searchQuery)}`
    try {
      const response = await fetch(url)
      this.suggestions = response.ok ? ((await response.json()) as PlaceSuggestion[]) : []
    } catch {
      this.suggestions = []
    }
  },

  choose(name: string, lat: number, lon: number) {
    this.selectedLat = lat
    this.selectedLon = lon
    this.selectedName = name
    this.searchQuery = name
    this.suggestions = []
    this.updateMap()
    void this.fetchResults()
  },

  selectSuggestion(suggestion: PlaceSuggestion) {
    this.choose(suggestion.name, suggestion.lat, suggestion.lon)
  },

  /** Stadtteil-Knopf: Name und Koordinaten aus den Datenattributen */
  selectDistrict(button: HTMLElement) {
    const lat = number(button.dataset.lat)
    const lon = number(button.dataset.lon)
    if (lat === null || lon === null) return
    this.choose(button.dataset.name ?? '', lat, lon)
  },

  setRadius(radius: number) {
    this.radius = radius
    if (this.selectedLat !== null) {
      this.updateMap()
      void this.fetchResults()
    }
  },

  /** Entfernt Ebenen von der Karte und gibt eine leere Liste zurück */
  removeLayers(layers: Layer[]): Layer[] {
    const map = this._map
    if (map) for (const layer of layers) map.removeLayer(layer)
    return []
  },

  updateMap() {
    const L = leaflet()
    const map = this._map
    if (!L || !map || this.selectedLat === null || this.selectedLon === null) return
    this._areaLayers = this.removeLayers(this._areaLayers)
    this._resultLayers = this.removeLayers(this._resultLayers)
    const center: LatLng = [this.selectedLat, this.selectedLon]
    const circle = L.circle(center, { radius: this.radius, className: 'search-radius', interactive: false }).addTo(map)
    this._areaLayers = [circle, L.circleMarker(center, CENTER_STYLE).addTo(map)]
    map.fitBounds(circle.getBounds(), { padding: [30, 30] })
  },

  async fetchResults() {
    const L = leaflet()
    const container = document.getElementById('results-container')
    if (this.selectedLat === null || this.selectedLon === null || !container) return
    this.loading = true
    const base = (this.$el as HTMLElement).dataset.resultsUrl ?? ''
    const url = `${base}?lat=${this.selectedLat}&lon=${this.selectedLon}&radius=${this.radius}`
    try {
      const response = await fetch(url, { headers: { 'HX-Request': 'true' } })
      if (!response.ok) throw new Error(String(response.status))
      container.innerHTML = await response.text()
      this._resultLayers = this.removeLayers(this._resultLayers)
      const map = this._map
      if (L && map) {
        for (const item of Array.from(container.querySelectorAll<HTMLElement>('[data-paper-lat]'))) {
          const lat = number(item.dataset.paperLat)
          const lon = number(item.dataset.paperLon)
          if (lat === null || lon === null) continue
          const marker = L.circleMarker([lat, lon], PLACE_STYLE)
          if (item.dataset.paperTitle) marker.bindPopup(popupContent(item.dataset.paperTitle))
          this._resultLayers.push(marker.addTo(map))
        }
      }
    } catch {
      container.textContent = ''
      const message = document.createElement('p')
      message.className = 'text-center text-sm text-red-700 dark:text-red-300 py-4'
      message.textContent = 'Die Ergebnisse konnten nicht geladen werden. Bitte versuchen Sie es erneut.'
      container.append(message)
    }
    this.loading = false
  },
}))
