/**
 * Karte der Vorgangsseite (templates/partials/paper_places.html, #598).
 *
 * Liest `#paper-map-data` (json_script) und zeichnet Orte als Punkte und amtliche Umringe von
 * Bebauungsplänen als Flächen. Leaflet kommt als Vendor-Skript (`static/vendor/leaflet/`) und steht
 * als `window.L` bereit. Texte aus den Daten (Ortsnamen, Quellenangaben) gelangen nie als HTML in die
 * Karte: Popups bekommen DOM-Knoten mit `textContent`, die Quellenangabe wird maskiert.
 */

import { readJsonScript } from './json-script'

type LatLng = [number, number]

interface Place {
  lat: number
  lon: number
  name: string
}

interface Area {
  name: string
  geometry: { type: string; coordinates: unknown }
}

interface PaperMapData {
  places: Place[]
  areas: Area[]
  attribution: string[]
}

// Ausschnitt der Leaflet-API, den diese Karte nutzt
interface LeafletBounds {
  extend(value: LatLng | LeafletBounds): LeafletBounds
  isValid(): boolean
}

export interface LeafletLayer {
  addTo(map: LeafletMap): LeafletLayer
  bindPopup(content: HTMLElement): LeafletLayer
}

interface LeafletGeoJson extends LeafletLayer {
  getBounds(): LeafletBounds
}

export interface LeafletMap {
  fitBounds(bounds: LeafletBounds, options?: Record<string, unknown>): LeafletMap
  setView(center: LatLng, zoom: number): LeafletMap
  attributionControl?: { addAttribution(text: string): unknown }
}

export interface LeafletStatic {
  map(element: HTMLElement, options: Record<string, unknown>): LeafletMap
  tileLayer(url: string, options: Record<string, unknown>): LeafletLayer
  circleMarker(latlng: LatLng, options: Record<string, unknown>): LeafletLayer
  geoJSON(data: unknown, options: Record<string, unknown>): LeafletGeoJson
  latLngBounds(latlngs: LatLng[]): LeafletBounds
}

/**
 * Kennfarbe als CSS-Farbe aus den Kanälen `--primary-<stufe>` (Issue #783): Grün im Bürgerportal, eigene Farbe
 * im Portal einer Körperschaft. Ohne Variable (Test, alter Stand) Grün 600.
 */
export function themeColor(stufe = 600): string {
  const kanal = getComputedStyle(document.documentElement).getPropertyValue(`--primary-${stufe}`).trim()
  const teile = kanal.split(/\s+/).filter(Boolean)
  return teile.length === 3 ? `rgb(${teile.join(', ')})` : '#17703f'
}

/** Punkt für Orte auf Karten (Vorgang, Sitzungsort, Nachbarschaft) in der Kennfarbe */
export function placeStyle(): Record<string, unknown> {
  return { radius: 7, color: '#ffffff', weight: 3, fillColor: themeColor(600), fillOpacity: 1 }
}

function escapeHtml(text: string): string {
  const element = document.createElement('span')
  element.textContent = text
  return element.innerHTML
}

export function popupContent(text: string): HTMLElement {
  const element = document.createElement('span')
  element.textContent = text
  return element
}

export function initPaperMap(): void {
  const element = document.getElementById('paper-map')
  const leaflet = (window as unknown as { L?: LeafletStatic }).L
  const data = readJsonScript<PaperMapData>('paper-map-data')
  if (!element || !leaflet || !data) return
  if (!data.places.length && !data.areas.length) {
    element.hidden = true
    return
  }

  const map = leaflet.map(element, {
    zoomControl: data.areas.length > 0,
    attributionControl: true,
    scrollWheelZoom: false,
    dragging: data.places.length + data.areas.length > 1 || data.areas.length > 0,
  })
  const tileUrl = (element.dataset.tileUrl ?? '').replace('/0/0/0', '/{z}/{x}/{y}')
  leaflet
    .tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    })
    .addTo(map)
  for (const text of data.attribution) {
    map.attributionControl?.addAttribution(escapeHtml(`Umring: ${text}`))
  }

  const bounds = leaflet.latLngBounds([])
  const style = placeStyle()
  const areaStyle = { color: themeColor(700), weight: 2, fillOpacity: 0.15 }
  for (const area of data.areas) {
    const layer = leaflet.geoJSON(area.geometry, { style: areaStyle })
    layer.bindPopup(popupContent(area.name))
    layer.addTo(map)
    bounds.extend(layer.getBounds())
  }
  for (const place of data.places) {
    const marker = leaflet.circleMarker([place.lat, place.lon], style)
    if (place.name) marker.bindPopup(popupContent(place.name))
    marker.addTo(map)
    bounds.extend([place.lat, place.lon])
  }

  if (!data.areas.length && data.places.length === 1) {
    map.setView([data.places[0].lat, data.places[0].lon], 15)
  } else if (bounds.isValid()) {
    map.fitBounds(bounds, { padding: [24, 24], maxZoom: 16 })
  }
}
