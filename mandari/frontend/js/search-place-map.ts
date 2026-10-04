/**
 * Karte im Ortsband der Suche (templates/partials/search_ortsband.html, Konzept Insight-Suche P0.8).
 *
 * Liest `#suche-ort-daten` (json_script: Mittelpunkt der erkannten Straße, Umkreis in Metern, Orte der
 * gezeigten Vorgänge) und zeichnet Umkreis, Straße und Vorgänge. Leaflet kommt als Vendor-Skript
 * (`static/vendor/leaflet/`). Nach jedem Austausch der Ergebnisse (HTMX) wird die neue Karte gezeichnet.
 * Ohne JavaScript bleibt die Liste daneben vollständig, der Link führt zur Kartenseite.
 */

import { readJsonScript } from './json-script'
import { type LeafletLayer, type LeafletMap, type LeafletStatic, placeStyle, themeColor } from './paper-map'

type LatLng = [number, number]

interface SearchPlaceData {
  center: LatLng
  radius: number
  name: string
  points: LatLng[]
}

type LeafletWithCircle = LeafletStatic & {
  circle(latlng: LatLng, options: Record<string, unknown>): LeafletLayer
}

export function initSearchPlaceMap(): void {
  const element = document.getElementById('suche-ortskarte')
  const leaflet = (window as unknown as { L?: LeafletWithCircle }).L
  const data = readJsonScript<SearchPlaceData>('suche-ort-daten')
  if (!element || !leaflet || !data || element.dataset.gezeichnet) return
  element.dataset.gezeichnet = 'ja'

  const map: LeafletMap = leaflet.map(element, {
    zoomControl: true,
    attributionControl: true,
    scrollWheelZoom: false,
  })
  const tileUrl = (element.dataset.tileUrl ?? '').replace('/0/0/0', '/{z}/{x}/{y}')
  leaflet
    .tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18,
    })
    .addTo(map)

  const umkreis = leaflet.circle(data.center, {
    radius: data.radius,
    color: themeColor(700),
    weight: 2,
    dashArray: '6 6',
    fill: false,
  })
  umkreis.addTo(map)
  for (const punkt of data.points) {
    leaflet.circleMarker(punkt, { ...placeStyle(), radius: 5, weight: 2 }).addTo(map)
  }
  leaflet.circleMarker(data.center, { ...placeStyle(), radius: 8, fillColor: themeColor(800) }).addTo(map)
  // Ausschnitt aus dem Umkreis berechnen; getBounds() des Kreises braucht eine schon gesetzte Ansicht
  const [lat, lon] = data.center
  const dLat = data.radius / 111_320
  const dLon = data.radius / (111_320 * Math.max(Math.cos((lat * Math.PI) / 180), 0.01))
  map.fitBounds(
    leaflet.latLngBounds([
      [lat - dLat, lon - dLon],
      [lat + dLat, lon + dLon],
    ]),
    { padding: [12, 12] },
  )
}

document.addEventListener('htmx:afterSettle', () => {
  try {
    initSearchPlaceMap()
  } catch (error) {
    // Die Karte ist Beiwerk: Liste und Link zur Kartenseite bleiben nutzbar
    console.warn('Karte im Ortsband nicht gezeichnet', error)
  }
})
