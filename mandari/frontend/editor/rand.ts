/**
 * Kommentare am Rand und Gliederung des neuen Antragseditors (Teil von #856).
 *
 * - `randAnordnen`: Karten neben ihre Textstelle setzen, ohne Überlappung; die aktive Karte steht genau neben
 *   ihrer Stelle, die übrigen weichen nach oben bzw. unten aus (wie in Google Docs).
 * - `gliederungAus`: Überschriften des Dokuments für die Gliederung links.
 */

/** Abstand zwischen zwei Karten am Rand (px) */
const KARTEN_ABSTAND = 8

export interface GliederungsEintrag {
  /** Nummer der Überschrift im Text (h1–h3 in Reihenfolge) */
  index: number
  text: string
  ebene: number
}

/**
 * Karten (`[data-rand-karte="<markId>"]`) im Container `rand` auf die Höhe ihrer Kommentarmarke im Text setzen.
 * Karten ohne Marke im Text werden ausgeblendet (`hidden`). Gibt die belegte Höhe zurück.
 */
export function randAnordnen(rand: HTMLElement, text: HTMLElement, aktiv: string | null): number {
  const karten = Array.from(rand.querySelectorAll<HTMLElement>('[data-rand-karte]'))
  const basis = rand.getBoundingClientRect().top
  const sichtbar: { karte: HTMLElement; soll: number; hoehe: number; id: string }[] = []
  for (const karte of karten) {
    const id = karte.dataset.randKarte ?? ''
    const marke = text.querySelector<HTMLElement>(`[data-comment-id="${CSS.escape(id)}"]`)
    karte.hidden = !marke
    if (!marke) continue
    sichtbar.push({ karte, id, soll: marke.getBoundingClientRect().top - basis - 4, hoehe: karte.offsetHeight })
  }
  if (!sichtbar.length) return 0
  sichtbar.sort((a, b) => a.soll - b.soll)

  const pos = sichtbar.map((k) => k.soll)
  const start = Math.max(
    0,
    sichtbar.findIndex((k) => k.id === aktiv),
  )
  for (let i = start + 1; i < sichtbar.length; i++) {
    pos[i] = Math.max(sichtbar[i].soll, pos[i - 1] + sichtbar[i - 1].hoehe + KARTEN_ABSTAND)
  }
  for (let i = start - 1; i >= 0; i--) {
    pos[i] = Math.min(sichtbar[i].soll, pos[i + 1] - sichtbar[i].hoehe - KARTEN_ABSTAND)
  }
  const verschiebung = pos[0] < 0 ? -pos[0] : 0
  sichtbar.forEach((k, i) => {
    k.karte.style.top = `${pos[i] + verschiebung}px`
  })
  const letzte = sichtbar.length - 1
  return pos[letzte] + verschiebung + sichtbar[letzte].hoehe
}

/**
 * Überschriften des Textes in Reihenfolge (leere ausgenommen); `index` zählt alle Überschriften. Eine einzelne
 * Überschrift 1 am Anfang ist der Titel des Antrags und steht nicht in der Gliederung.
 */
export function gliederungAus(text: HTMLElement): GliederungsEintrag[] {
  const koepfe = Array.from(text.querySelectorAll<HTMLElement>('h1, h2, h3'))
  const titelAmAnfang = koepfe[0]?.tagName === 'H1' && koepfe.filter((k) => k.tagName === 'H1').length === 1
  const eintraege: GliederungsEintrag[] = []
  koepfe.forEach((kopf, index) => {
    const titel = (kopf.textContent ?? '').trim()
    if (titel && !(titelAmAnfang && index === 0)) {
      eintraege.push({ index, text: titel, ebene: Number(kopf.tagName.slice(1)) || 1 })
    }
  })
  return eintraege
}

/** Zur Überschrift Nummer `index` scrollen */
export function zuUeberschriftSpringen(text: HTMLElement, index: number): void {
  const kopf = text.querySelectorAll<HTMLElement>('h1, h2, h3')[index]
  kopf?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
