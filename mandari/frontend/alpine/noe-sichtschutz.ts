/**
 * Sichtschutz für Nichtöffentliches in ganz Work (Issues #1166, #1201): Alpine-Store `noe`.
 *
 * Titel nichtöffentlicher TOPs sind weichgezeichnet, ihre Inhalte abgedeckt, bis jemand Nichtöffentliches aktiv
 * einblendet. Der Zustand ist ein Cookie mit der Marke der Anmeldung (`apps/work/sichtschutz.py`): Es gilt für alle
 * Seiten und Tabs, jede Anmeldung beginnt verdeckt (neue Marke). Der Server liefert jede Seite schon richtig aus
 * (Klasse `noe-verdeckt` am `<body>`, Marke in `data-noe-marke`); dieser Store übernimmt den Anfangswert und schaltet:
 * - sofort im Browser (Klasse am `<body>`, `$store.noe.sichtbar` für Bindungen wie `aria-hidden` und `inert`),
 * - im Cookie, ohne Anfrage an den Server: Die nächste Seite kommt gleich richtig, auch wenn sie sofort geladen wird,
 * - in allen offenen Tabs per BroadcastChannel (beide Richtungen, ohne Neuladen; wichtig vor allem beim Verdecken).
 * Nur Sichtschutz: Die Rechte prüft der Server.
 *
 * Bedienung: Knopf in der Kopfleiste (`templates/work/rahmen/_noe_knopf.html`), Schalter der Sitzungsansicht
 * (`templates/work/faction/sitzung/_noe_schalter.html`) und Abdeckungen (`_noe_abdeckung.html`).
 */

import type { Alpine as AlpineType } from 'alpinejs'

const KANAL = 'mandari-noe-sichtschutz'
/** Name des Cookies, wie `COOKIE` in apps/work/sichtschutz.py */
const COOKIE = 'mandari_noe'
const VERDECKT = 'noe-verdeckt'

export interface NoeStore {
  sichtbar: boolean
  init(): void
  umschalten(): void
  einblenden(): void
  verdecken(): void
  setzen(sichtbar: boolean): void
  uebernehmen(sichtbar: boolean): void
}

interface Nachricht {
  sichtbar?: unknown
}

let kanal: BroadcastChannel | null = null

function oeffnen(): BroadcastChannel | null {
  if (kanal || typeof BroadcastChannel === 'undefined') return kanal
  try {
    kanal = new BroadcastChannel(KANAL)
  } catch {
    kanal = null
  }
  return kanal
}

/** Marke der Anmeldung (nur Hex-Zeichen); leer ohne Anmeldung */
function marke(): string {
  const wert = document.body?.dataset.noeMarke ?? ''
  return /^[0-9a-f]+$/.test(wert) ? wert : ''
}

function cookieSchreiben(sichtbar: boolean): void {
  const sicher = window.location.protocol === 'https:' ? '; Secure' : ''
  const wert = marke()
  // Ohne Ablaufdatum: endet mit dem Browser. Verdecken löscht immer, auch ohne Marke.
  // biome-ignore lint/suspicious/noDocumentCookie: Ein Cookie, das der Server nur liest; Cookie Store API fehlt in Firefox/Safari
  document.cookie =
    sichtbar && wert
      ? `${COOKIE}=${wert}; Path=/; SameSite=Lax${sicher}`
      : `${COOKIE}=; Path=/; Max-Age=0; SameSite=Lax${sicher}`
}

/** Trägt das Cookie die Marke dieser Anmeldung? */
function cookieSichtbar(): boolean {
  const wert = marke()
  return !!wert && document.cookie.split(/;\s*/).includes(`${COOKIE}=${wert}`)
}

function klasseSetzen(sichtbar: boolean): void {
  document.body?.classList.toggle(VERDECKT, !sichtbar)
}

export function createNoeStore(): NoeStore {
  return {
    sichtbar: false,

    init() {
      // Vom Server eingeblendet, aber das Cookie ist inzwischen weg (in einem anderen Tab verdeckt): verdeckt bleiben
      const body = document.body
      this.uebernehmen(!!body && !body.classList.contains(VERDECKT) && cookieSichtbar())
      // `this` ist hier der reaktive Store (Alpine ruft init daran auf); die Pfeilfunktionen unten behalten ihn
      oeffnen()?.addEventListener('message', (event: MessageEvent<Nachricht>) => {
        if (typeof event.data?.sichtbar === 'boolean') this.uebernehmen(event.data.sichtbar)
      })
      // Seite aus dem Zurück-Cache: Nachrichten der Zwischenzeit fehlen, das Cookie hat den aktuellen Stand
      window.addEventListener('pageshow', (event) => {
        if (event.persisted) this.uebernehmen(cookieSichtbar())
      })
    },

    umschalten() {
      this.setzen(!this.sichtbar)
    },

    einblenden() {
      this.setzen(true)
    },

    verdecken() {
      this.setzen(false)
    },

    /** Selbst umgeschaltet: Cookie schreiben, hier anwenden, anderen Tabs melden */
    setzen(sichtbar: boolean) {
      cookieSchreiben(sichtbar)
      this.uebernehmen(sichtbar)
      try {
        oeffnen()?.postMessage({ sichtbar })
      } catch {
        // Kanal geschlossen: die übrigen Tabs gleichen beim nächsten Laden über das Cookie ab
      }
    },

    /** Zustand nur hier anwenden (Anfangswert, Nachricht eines anderen Tabs) */
    uebernehmen(sichtbar: boolean) {
      this.sichtbar = sichtbar
      klasseSetzen(sichtbar)
    },
  }
}

export function registerNoeStore(Alpine: AlpineType): void {
  Alpine.store('noe', createNoeStore())
}

/** Seite „Abgemeldet“: Cookie löschen und offene Tabs sofort verdecken (die nächste Anmeldung hat ohnehin eine neue Marke). */
export function nachAbmeldungVerdecken(): void {
  cookieSchreiben(false)
  try {
    oeffnen()?.postMessage({ sichtbar: false })
  } catch {
    // ohne Kanal: offene Tabs der alten Anmeldung zeigen beim nächsten Laden die Anmeldung
  }
}
