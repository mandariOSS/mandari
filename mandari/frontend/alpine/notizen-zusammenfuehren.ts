/**
 * Zwei Fassungen der Notizen eines TOPs zusammenführen (Fraktionssitzung, Issue #1166).
 *
 * Gebraucht, wenn der Server eine Speicherung ablehnt, weil inzwischen jemand anders gespeichert hat (409), und wenn
 * der Live-Editor nach einer Speicherung ohne Verbindung neu aufbaut. Bisher standen dann immer beide Fassungen
 * untereinander – auch wenn sie gleich waren oder die eine nur ein älterer Stand der anderen war (es wurde einfach
 * weitergetippt). So entstand derselbe Satz zwei- und dreimal mit „Ihre gleichzeitige Fassung:“ dazwischen.
 *
 * Regeln (es geht nie Text verloren, höchstens kommt eine Löschung zurück):
 * 1. Gleiches HTML: kein Konflikt.
 * 2. Mit bekannter Basis (Stand, auf dem die eigene Fassung beruht): Eigener Text unverändert → Server; Text auf dem
 *    Server unverändert → eigene Fassung.
 * 3. Gleicher Text (nur die Formatierung weicht ab) → eigene Fassung.
 * 4. Eine Seite leer → die andere.
 * 5. Ist eine Fassung Anfang oder Ende der anderen, gilt die längere (die kürzere ist ein älterer Stand: es wurde
 *    weitergetippt, oder die andere hat schon zusammengeführt). Steht sie nur irgendwo mittendrin, ist es ein
 *    Konflikt, damit eine kurze eigene Eingabe nicht unbemerkt wegfällt.
 * 6. Sonst stehen beide untereinander, getrennt durch die Konfliktmarke.
 *
 * Reine Funktionen ohne DOM, damit sie unter Node getestet werden können (`frontend/js/test/`).
 */

export const KONFLIKT_MARKE = '<p><strong>Ihre gleichzeitige Fassung:</strong></p>'

export interface Zusammenfuehrung {
  html: string
  /** Beide Fassungen stehen untereinander (echter Konflikt) */
  konflikt: boolean
}

const ENTITAETEN: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

/** Reiner Text eines HTML-Stücks, Leerraum vereinheitlicht (Blöcke und Zeilenumbrüche zählen als Leerzeichen). */
export function notizText(html: string): string {
  return (html || '')
    .replace(/<[^>]*>/g, ' ')
    .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (ganz, name: string) => {
      if (name[0] === '#') {
        const zahl = name[1] === 'x' || name[1] === 'X' ? Number.parseInt(name.slice(2), 16) : Number(name.slice(1))
        return Number.isFinite(zahl) && zahl > 0 ? String.fromCodePoint(zahl) : ganz
      }
      return ENTITAETEN[name.toLowerCase()] ?? ganz
    })
    .replace(/\s+/g, ' ')
    .trim()
}

/** Hat das HTML Inhalt (Text oder wenigstens eine Liste, die der Editor auch leer anlegt)? */
function hatInhalt(html: string, text: string): boolean {
  return Boolean(text) || /<li[\s>]/i.test(html || '')
}

/**
 * Eigene Fassung mit der Fassung des Servers zusammenführen.
 *
 * @param eigene Inhalt dieses Editors
 * @param server Gespeicherter Stand auf dem Server
 * @param basis Stand, auf dem die eigene Fassung beruht (zuletzt geladen oder gespeichert); `null`, wenn unbekannt
 */
export function zusammenfuehren(eigene: string, server: string, basis: string | null = null): Zusammenfuehrung {
  const ohneKonflikt = (html: string): Zusammenfuehrung => ({ html, konflikt: false })
  if (eigene === server) return ohneKonflikt(server)
  const e = notizText(eigene)
  const s = notizText(server)
  if (basis !== null) {
    const b = notizText(basis)
    if (e === b) return ohneKonflikt(server)
    if (s === b) return ohneKonflikt(eigene)
  }
  if (e === s) return ohneKonflikt(eigene)
  if (!hatInhalt(eigene, e)) return ohneKonflikt(server)
  if (!hatInhalt(server, s)) return ohneKonflikt(eigene)
  if (s.startsWith(e) || s.endsWith(e)) return ohneKonflikt(server)
  if (e.startsWith(s) || e.endsWith(s)) return ohneKonflikt(eigene)
  return { html: `${server}${KONFLIKT_MARKE}${eigene}`, konflikt: true }
}
