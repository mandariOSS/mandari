/**
 * Sichtschutz für Nichtöffentliches in ganz Work (Issues #1166, #1201): Alpine-Store `noe`.
 *
 * Titel nichtöffentlicher TOPs sind weichgezeichnet, ihre Inhalte abgedeckt, bis jemand Nichtöffentliches aktiv
 * einblendet. Der Zustand gilt für die angemeldete Sitzung (Django-Session, `apps/work/sichtschutz.py`); jede
 * Anmeldung beginnt verdeckt. Der Server liefert jede Seite schon richtig aus (Klasse `noe-verdeckt` am `<body>`),
 * dieser Store übernimmt den Anfangswert daraus und schaltet danach:
 * - sofort im Browser (Klasse am `<body>`, `$store.noe.sichtbar` für Bindungen wie `aria-hidden` und `inert`),
 * - in allen offenen Tabs per BroadcastChannel (beide Richtungen, ohne Neuladen; wichtig vor allem beim Verdecken),
 * - auf dem Server per POST, damit die nächste Seite gleich richtig kommt.
 * Verdecken gilt lokal sofort, auch wenn der POST scheitert; bis er bestätigt ist, merkt sich der Browser das
 * Verdecken und holt es auf jeder Seite nach. Nur Sichtschutz: Die Rechte prüft der Server.
 *
 * Bedienung: Knopf in der Kopfleiste (`templates/work/rahmen/_noe_knopf.html`), Schalter der Sitzungsansicht
 * (`templates/work/faction/sitzung/_noe_schalter.html`) und Abdeckungen (`_noe_abdeckung.html`).
 */

import type { Alpine as AlpineType } from 'alpinejs'
import { csrfTokenAktuell } from '../js/csrf'

const KANAL = 'mandari-noe-sichtschutz'
/** Verdecken angefordert, Server hat es noch nicht bestätigt (localStorage, für alle Tabs) */
const AUSSTEHEND = 'mandari-noe-verdecken-ausstehend'
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

function adresse(): string {
  return document.body?.dataset.noeUrl ?? ''
}

function ausstehend(): boolean {
  try {
    return window.localStorage.getItem(AUSSTEHEND) === '1'
  } catch {
    return false
  }
}

function ausstehendMerken(an: boolean): void {
  try {
    if (an) window.localStorage.setItem(AUSSTEHEND, '1')
    else window.localStorage.removeItem(AUSSTEHEND)
  } catch {
    // Gesperrter Speicher: Verdecken gilt trotzdem sofort, nur das Nachholen entfällt
  }
}

function klasseSetzen(sichtbar: boolean): void {
  document.body?.classList.toggle(VERDECKT, !sichtbar)
}

// Anfragen nacheinander senden: Sonst könnte ein späteres Verdecken vor einem früheren Einblenden ankommen
let kette: Promise<unknown> = Promise.resolve()

function senden(sichtbar: boolean): Promise<boolean> {
  const ziel = adresse()
  if (!ziel) return Promise.resolve(false)
  const anfrage = kette.then(async () => {
    const body = new FormData()
    body.append('sichtbar', sichtbar ? '1' : '0')
    try {
      const antwort = await fetch(ziel, {
        method: 'POST',
        body,
        credentials: 'same-origin',
        // Auch beim gleich folgenden Seitenwechsel zu Ende senden
        keepalive: true,
        headers: { 'X-CSRFToken': csrfTokenAktuell(), 'X-Requested-With': 'XMLHttpRequest' },
      })
      return antwort.ok && antwort.headers.get('content-type')?.includes('json') === true
    } catch {
      return false
    }
  })
  kette = anfrage
  return anfrage
}

/** Verdecken an den Server senden; erst nach seiner Bestätigung gilt es als erledigt. */
function verdeckenSenden(): void {
  ausstehendMerken(true)
  void senden(false).then((ok) => {
    if (ok) ausstehendMerken(false)
  })
}

export function createNoeStore(): NoeStore {
  return {
    sichtbar: false,

    init() {
      const body = document.body
      const sichtbar = !!body && !body.classList.contains(VERDECKT)
      if (sichtbar && ausstehend()) {
        // Verdecken in einem Tab, das der Server noch nicht bestätigt hat: hier nachholen
        this.uebernehmen(false)
        verdeckenSenden()
      } else {
        this.uebernehmen(sichtbar)
      }
      // `this` ist hier der reaktive Store (Alpine ruft init daran auf); die Pfeilfunktionen unten behalten ihn
      oeffnen()?.addEventListener('message', (event: MessageEvent<Nachricht>) => {
        if (typeof event.data?.sichtbar === 'boolean') this.uebernehmen(event.data.sichtbar)
      })
      // Seite aus dem Zurück-Cache: Nachrichten der Zwischenzeit fehlen, deshalb beim Server nachfragen
      window.addEventListener('pageshow', (event) => {
        if (!event.persisted || !adresse()) return
        if (ausstehend()) {
          this.uebernehmen(false)
          return
        }
        fetch(adresse(), { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
          .then((antwort) => (antwort.ok ? antwort.json() : null))
          .then((daten: Nachricht | null) => {
            if (typeof daten?.sichtbar === 'boolean') this.uebernehmen(daten.sichtbar)
          })
          .catch(() => {
            // Ohne Antwort bleibt der Stand; Verdecken aus anderen Tabs kam per Kanal oder steht oben aus
          })
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

    /** Selbst umgeschaltet: hier anwenden, anderen Tabs melden, dem Server senden */
    setzen(sichtbar: boolean) {
      this.uebernehmen(sichtbar)
      try {
        oeffnen()?.postMessage({ sichtbar })
      } catch {
        // Kanal geschlossen: die übrigen Tabs gleichen beim nächsten Laden ab
      }
      if (sichtbar) {
        ausstehendMerken(false)
        void senden(true)
      } else {
        verdeckenSenden()
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

/** Seite „Abgemeldet“: offene Tabs verdecken sofort (die nächste Anmeldung beginnt ohnehin verdeckt). */
export function nachAbmeldungVerdecken(): void {
  ausstehendMerken(false)
  try {
    oeffnen()?.postMessage({ sichtbar: false })
  } catch {
    // ohne Kanal: offene Tabs der alten Anmeldung zeigen beim nächsten Laden die Anmeldung
  }
}
