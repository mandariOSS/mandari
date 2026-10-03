/**
 * Karte des Sitzungsorts (templates/pages/meetings/_details_card.html, vorher Inline-Skript).
 *
 * Liest Koordinaten, Ort, Adresse und Kachel-URL aus den Datenattributen von `#location-map` und setzt
 * einen Punkt wie auf der Vorgangsseite. Ort und Adresse stammen aus der Quelle und gelangen nur als Text
 * in das Popup, nie als HTML.
 */

import { type LeafletStatic, placeStyle } from './paper-map'

function popupFor(name: string, address: string): HTMLElement {
  const popup = document.createElement('div')
  const title = document.createElement('strong')
  title.textContent = name
  popup.append(title)
  if (address) {
    const line = document.createElement('div')
    line.textContent = address
    popup.append(line)
  }
  return popup
}

export function initMeetingMap(): void {
  const element = document.getElementById('location-map')
  const leaflet = (window as unknown as { L?: LeafletStatic }).L
  if (!element || !leaflet) return
  const lat = Number.parseFloat(element.dataset.lat ?? '')
  const lng = Number.parseFloat(element.dataset.lng ?? '')
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return

  const map = leaflet.map(element, { zoomControl: true, scrollWheelZoom: false, doubleClickZoom: true })
  map.setView([lat, lng], 16)
  const tileUrl = (element.dataset.tileUrl ?? '').replace('/0/0/0', '/{z}/{x}/{y}')
  leaflet
    .tileLayer(tileUrl, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    })
    .addTo(map)

  const marker = leaflet.circleMarker([lat, lng], placeStyle())
  const name = element.dataset.locationName ?? ''
  if (name) marker.bindPopup(popupFor(name, element.dataset.locationAddress ?? ''))
  marker.addTo(map)
}
