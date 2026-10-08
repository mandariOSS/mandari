/**
 * Gemeinsames Kartenmodul für Vorgänge mit Ortsbezug (Issue #853, docs/CSP.md Abschnitt 4): die Karte in Insight
 * (templates/pages/map.html, Alpine-Komponente `insightKarte`) und die Karte der Recherche in Work
 * (templates/work/ris/partials/_karte.html, `risKarte`). Vorher stand die Karte von Insight als Inline-Skript in
 * der Seite, Work hatte eine eigene Kopie.
 *
 * - `karteAnlegen`: Karte mit Startausschnitt (Rahmen der Kommune, sonst Zentrum) und Kacheln über den Kachel-Proxy
 *   von mandari – der Browser fragt nie einen fremden Kartendienst an.
 * - `punktGruppe`: Ebene für die Punkte, mit Marker-Cluster, wenn das Vendor-Skript geladen ist.
 * - `PunkteAbfrage`: lädt die Punkte (GeoJSON aus `insight_core.services.karten_punkte`) je Ausschnitt; eine neue
 *   Anfrage bricht die vorige ab.
 * - `punkteZeichnen`: Punkte mit Popup aus DOM-Knoten; Titel und Ortsnamen gelangen nur als Text hinein, nie als HTML.
 *
 * Leaflet und Marker-Cluster kommen als Vendor-Skripte (`static/vendor/leaflet/`) und stehen als `window.L` bereit.
 */

export type LatLng = [number, number]

export interface Rahmen {
  north: number | null
  south: number | null
  east: number | null
  west: number | null
}

export interface KartenStart {
  /** Rahmen der Kommune; hat er alle vier Seiten, gilt er vor dem Zentrum */
  rahmen?: Rahmen | null
  zentrum?: LatLng | null
  /** Zoom zum Zentrum (ohne Zentrum: Deutschland) */
  zoom?: number
  /** Rand beim Einpassen in den Rahmen (px) */
  rand?: number
  /** Höchster Zoom beim Einpassen in den Rahmen */
  hoechsterZoom?: number
}

export interface KarteFeature {
  geometry: { coordinates: [number, number] }
  properties: {
    id: string
    title: string
    reference: string | null
    date?: string | null
    location_name: string
    /** Adresse des Vorgangs, wenn der Server sie mitgibt (Insight) */
    url?: string
  }
}

export interface KarteAntwort {
  features: KarteFeature[]
  truncated: boolean
}

export interface Ausschnitt {
  toBBoxString(): string
}

export interface Ebene {
  addTo(map: KarteMap): Ebene
  bindPopup(content: HTMLElement, options?: Record<string, unknown>): Ebene
  on(event: string, handler: () => void): Ebene
  /** DOM-Element des Markers, sobald er auf der Karte liegt (nur Marker, nicht Kreise) */
  getElement?(): HTMLElement | undefined
}

export interface Gruppe extends Ebene {
  clearLayers(): Gruppe
  addLayer(layer: Ebene): Gruppe
  getLayers(): Ebene[]
  getBounds(): unknown
}

export interface KarteMap {
  setView(center: LatLng, zoom: number): KarteMap
  fitBounds(bounds: unknown, options?: Record<string, unknown>): KarteMap
  getBounds(): Ausschnitt
  on(event: string, handler: () => void): KarteMap
}

export interface LeafletApi {
  map(element: HTMLElement, options: Record<string, unknown>): KarteMap
  tileLayer(url: string, options: Record<string, unknown>): Ebene
  circleMarker(latlng: LatLng, options: Record<string, unknown>): Ebene
  marker(latlng: LatLng, options: Record<string, unknown>): Ebene
  divIcon(options: Record<string, unknown>): unknown
  layerGroup(): Gruppe
  markerClusterGroup?: (options: Record<string, unknown>) => Gruppe
  latLngBounds(southWest: LatLng, northEast: LatLng): unknown
}

/** Mitte Deutschlands, wenn eine Kommune weder Rahmen noch Zentrum hat */
export const DEUTSCHLAND: LatLng = [51.1657, 10.4515]
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'

/** Leaflet aus dem Vendor-Skript, sonst `undefined` (Seite ohne Karte, Skript nicht geladen) */
export function leaflet(): LeafletApi | undefined {
  return (window as unknown as { L?: LeafletApi }).L
}

/** Hat der Rahmen alle vier Seiten? */
export function vollerRahmen(rahmen: Rahmen | null | undefined): rahmen is Rahmen & Record<keyof Rahmen, number> {
  return rahmen?.north != null && rahmen.south != null && rahmen.east != null && rahmen.west != null
}

/**
 * Karte auf `flaeche` mit Startausschnitt und Kacheln über den Kachel-Proxy. `kacheln` ist die Adresse der Kachel
 * 0/0/0 (`{% url 'insight_core:insight:tile_proxy' z=0 x=0 y=0 %}`), die Karte setzt die Platzhalter ein.
 */
export function karteAnlegen(L: LeafletApi, flaeche: HTMLElement, kacheln: string, start: KartenStart): KarteMap {
  const map = L.map(flaeche, { zoomControl: true, attributionControl: true })
  const rahmen = start.rahmen
  if (vollerRahmen(rahmen)) {
    const optionen: Record<string, unknown> = { padding: [start.rand ?? 24, start.rand ?? 24] }
    if (start.hoechsterZoom) optionen.maxZoom = start.hoechsterZoom
    map.fitBounds(L.latLngBounds([rahmen.south, rahmen.west], [rahmen.north, rahmen.east]), optionen)
  } else if (start.zentrum) {
    map.setView(start.zentrum, start.zoom ?? 12)
  } else {
    map.setView(DEUTSCHLAND, 6)
  }
  L.tileLayer(kacheln.replace('/0/0/0', '/{z}/{x}/{y}'), { attribution: ATTRIBUTION, maxZoom: 18 }).addTo(map)
  return map
}

/** Ebene für die Punkte: Marker-Cluster, wenn geladen, sonst eine einfache Gruppe */
export function punktGruppe(L: LeafletApi, map: KarteMap, clusterRadius: number): Gruppe {
  const gruppe = L.markerClusterGroup
    ? L.markerClusterGroup({ maxClusterRadius: clusterRadius, spiderfyOnMaxZoom: true, showCoverageOnHover: false })
    : L.layerGroup()
  gruppe.addTo(map)
  return gruppe
}

/** Kartenausschnitt als `west,süd,ost,nord` für den Parameter `bbox` */
export function ausschnitt(map: KarteMap): string {
  return map.getBounds().toBBoxString()
}

/** Lädt die Punkte; eine neue Anfrage bricht die vorige ab (dann liefert `laden` `null`). */
export class PunkteAbfrage {
  private abbruch: AbortController | undefined

  async laden(adresse: string, params: URLSearchParams): Promise<KarteAntwort | null> {
    this.abbruch?.abort()
    const abbruch = new AbortController()
    this.abbruch = abbruch
    try {
      const antwort = await fetch(`${adresse}?${params}`, {
        signal: abbruch.signal,
        headers: { Accept: 'application/json' },
      })
      if (!antwort.ok) throw new Error(String(antwort.status))
      const daten = (await antwort.json()) as Partial<KarteAntwort>
      return { features: daten.features ?? [], truncated: Boolean(daten.truncated) }
    } catch (fehler) {
      if ((fehler as Error).name === 'AbortError') return null
      throw fehler
    }
  }
}

/**
 * Zeichnet die Punkte neu: `punkt` erzeugt die Ebene je Ort, `popup` ihren Inhalt aus DOM-Knoten. Marker (Leaflet
 * setzt `role="button"`) bekommen den Titel des Vorgangs als Namen für Bildschirmleser, sobald sie auf der Karte liegen
 * – auch wenn ein Cluster sie erst beim Hineinzoomen zeigt.
 */
export function punkteZeichnen(
  gruppe: Gruppe,
  features: KarteFeature[],
  punkt: (lage: LatLng, feature: KarteFeature) => Ebene,
  popup: (feature: KarteFeature) => HTMLElement,
  popupOptionen: Record<string, unknown> = {},
): void {
  gruppe.clearLayers()
  for (const feature of features) {
    const [lon, lat] = feature.geometry.coordinates
    const ebene = punkt([lat, lon], feature)
    const name = feature.properties.title || 'Vorgang'
    ebene.on('add', () => {
      const element = ebene.getElement?.()
      if (element?.getAttribute('role') === 'button') element.setAttribute('aria-label', name)
    })
    ebene.bindPopup(popup(feature), popupOptionen)
    gruppe.addLayer(ebene)
  }
}

/** Element mit Klassen und Text (Text nie als HTML) */
export function knoten<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  klassen: string,
  text = '',
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag)
  if (klassen) element.className = klassen
  if (text) element.textContent = text
  return element
}
