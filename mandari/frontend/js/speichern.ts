/**
 * Speichern mit Wiederholung (#854): gemeinsame Speicherlogik der Sitzungsvorbereitung.
 *
 * Bisher ging eine Eingabe verloren, wenn ein Speichern scheiterte (Neustart beim Deploy, kurze Netzstörung):
 * Die Oberfläche zeigte „Nicht gespeichert“, wiederholte aber nichts. Hier gilt:
 *
 * - Aufträge mit Schlüssel (Feld eines Ziels, z. B. Position oder Notiz zu einem TOP) sind Aktualisierungen.
 *   Je Schlüssel läuft höchstens eine Anfrage; kommt ein neuerer Stand, ersetzt er den noch nicht gesendeten.
 *   So überschreibt nie ein älterer Stand einen neueren. Bei Störung (keine Antwort, 408/425/429/502/503/504)
 *   wird mit steigender Wartezeit wiederholt, solange die Seite offen ist.
 * - Abgelaufene Anmeldung bzw. fehlender zweiter Faktor halten die Aufträge an; sie werden erneut gesendet, sobald
 *   die Seite wieder Fokus bekommt, und sonst alle 30 s (Anmeldung in einem anderen Tab genügt).
 * - Aufträge ohne Schlüssel (Anlegen, Löschen, Hochladen) werden genau einmal gesendet: Eine Wiederholung könnte
 *   doppelt anlegen. Die Eingabe bleibt beim Aufrufer stehen.
 * - Endgültige Fehler (400, 403, 404, 500 …) werden nicht wiederholt.
 *
 * Das Modul kennt kein Alpine; der Stand wird über `beiAenderung` gemeldet.
 */

import { csrfTokenAktuell } from './csrf'

// biome-ignore lint/suspicious/noExplicitAny: Antworten der JSON-Endpunkte sind nicht schematisiert
export type JsonAntwort = Record<string, any>

export interface SpeicherAuftrag {
  url: string
  method?: string
  /** JSON-Objekt oder FormData */
  body?: unknown
  /** Gleiches Ziel: nur der neueste Stand wird gesendet, der Reihe nach, mit Wiederholung bei Störung */
  schluessel?: string
  /** Für Meldungen, z. B. „Position“ oder „Redebeitrag“ */
  bezeichnung?: string
}

export type SpeicherErgebnis =
  | { ok: true; daten: JsonAntwort }
  | { ok: false; ersetzt: true }
  | { ok: false; ersetzt: false; meldung: string }

export type Anmeldeproblem = '' | 'abgelaufen' | 'zweiter_faktor'

export interface SpeicherStand {
  /** Anfragen unterwegs bzw. direkt dahinter eingereiht */
  sendet: number
  /** Aufträge, die nach einer Störung noch nicht gespeichert sind (warten oder werden gerade wiederholt) */
  wiederholt: number
  offline: boolean
  anmeldung: Anmeldeproblem
  /** Adresse zum Einrichten des zweiten Faktors (aus der Antwort des Servers) */
  anmeldungZiel: string
  /** Endgültig gescheitert (nicht wiederholbar); leer, wenn nichts offen ist */
  fehler: string
  zuletztGespeichert: Date | null
}

export interface SpeicherOptionen {
  beiAenderung?: (stand: SpeicherStand) => void
  /** Wartezeiten zwischen den Versuchen; der letzte Wert gilt für alle weiteren */
  wartezeitenMs?: number[]
  /** Abstand der Versuche bei abgelaufener Anmeldung */
  anmeldungWartezeitMs?: number
  fetch?: typeof fetch
}

const WARTEZEITEN_MS = [1000, 2000, 4000, 8000, 15000, 30000]
const ANMELDUNG_WARTEZEIT_MS = 30000
/** Antworten, bei denen die Anfrage nicht (sicher) verarbeitet wurde und eine Wiederholung hilft */
const VORUEBERGEHEND = new Set([408, 425, 429, 502, 503, 504])
/** Browser begrenzen keepalive-Anfragen auf 64 KiB je Seite */
const KEEPALIVE_GRENZE = 60000

interface Eintrag {
  auftrag: SpeicherAuftrag
  versuche: number
  /** gerade unterwegs mit keepalive (überlebt das Verlassen der Seite) */
  keepalive: boolean
  aufloesen: (ergebnis: SpeicherErgebnis) => void
}

interface Platz {
  laufend: Eintrag | null
  naechster: Eintrag | null
  timer: ReturnType<typeof setTimeout> | null
}

type Antwortart =
  | { art: 'ok'; daten: JsonAntwort }
  | { art: 'stoerung' }
  | { art: 'anmeldung'; problem: Exclude<Anmeldeproblem, ''>; ziel: string }
  | { art: 'fehler'; meldung: string }

function istJson(resp: Response): boolean {
  return (resp.headers.get('Content-Type') || '').includes('application/json')
}

async function leseJson(resp: Response): Promise<JsonAntwort | null> {
  try {
    return (await resp.json()) as JsonAntwort
  } catch {
    return null
  }
}

async function bewerte(resp: Response): Promise<Antwortart> {
  // Abgelaufene Anmeldung: Django leitet auf die Anmeldeseite um, fetch folgt und liefert HTML
  if (resp.redirected && !istJson(resp)) return { art: 'anmeldung', problem: 'abgelaufen', ziel: '' }
  if (resp.status === 401) return { art: 'anmeldung', problem: 'abgelaufen', ziel: '' }
  if (resp.status === 403) {
    const daten = istJson(resp) ? await leseJson(resp) : null
    if (daten?.error === 'two_factor_setup_required') {
      return { art: 'anmeldung', problem: 'zweiter_faktor', ziel: String(daten.redirect || '') }
    }
    // 403 als HTML = CSRF-Prüfung (Token nach neuer Anmeldung in einem anderen Tab gewechselt)
    if (!daten) return { art: 'anmeldung', problem: 'abgelaufen', ziel: '' }
    return { art: 'fehler', meldung: typeof daten.error === 'string' ? daten.error : 'keine Berechtigung' }
  }
  if (VORUEBERGEHEND.has(resp.status)) return { art: 'stoerung' }
  if (!resp.ok) {
    const daten = istJson(resp) ? await leseJson(resp) : null
    const meldung = typeof daten?.error === 'string' ? daten.error : `Fehler ${resp.status}`
    return { art: 'fehler', meldung }
  }
  const daten = await leseJson(resp)
  if (!daten) return { art: 'fehler', meldung: 'unerwartete Antwort des Servers' }
  return { art: 'ok', daten }
}

export class Speicherdienst {
  /** Seite wird verborgen bzw. verlassen: kleine Anfragen mit keepalive senden */
  verbergen = false

  private plaetze = new Map<string, Platz>()
  /** Endgültig gescheiterte Aufträge je Schlüssel (bis derselbe Schlüssel gespeichert wird) */
  private fehlgeschlagen = new Map<string, string>()
  private zaehler = 0
  private anmeldung: Anmeldeproblem = ''
  private anmeldungZiel = ''
  private offline = false
  private zuletztGespeichert: Date | null = null
  private readonly beiAenderung: (stand: SpeicherStand) => void
  private readonly wartezeiten: number[]
  private readonly anmeldungWartezeit: number
  private readonly abrufen: typeof fetch

  constructor(optionen: SpeicherOptionen = {}) {
    this.beiAenderung = optionen.beiAenderung || (() => {})
    this.wartezeiten = optionen.wartezeitenMs?.length ? optionen.wartezeitenMs : WARTEZEITEN_MS
    this.anmeldungWartezeit = optionen.anmeldungWartezeitMs ?? ANMELDUNG_WARTEZEIT_MS
    this.abrufen = optionen.fetch || ((...args: Parameters<typeof fetch>) => fetch(...args))
  }

  /**
   * Auftrag senden. Die Anfrage startet synchron (wichtig beim Verlassen der Seite), außer derselbe Schlüssel
   * ist gerade unterwegs; dann folgt sie direkt danach. Das Ergebnis kommt erst, wenn gespeichert, endgültig
   * gescheitert oder durch einen neueren Stand ersetzt.
   */
  senden(auftrag: SpeicherAuftrag): Promise<SpeicherErgebnis> {
    const schluessel = auftrag.schluessel || `einzel-${++this.zaehler}`
    return new Promise<SpeicherErgebnis>((aufloesen) => {
      const eintrag: Eintrag = { auftrag, versuche: 0, keepalive: false, aufloesen }
      let platz = this.plaetze.get(schluessel)
      if (!platz) {
        platz = { laufend: null, naechster: null, timer: null }
        this.plaetze.set(schluessel, platz)
      }
      if (platz.naechster) {
        // Älterer, noch nicht gesendeter Stand: der neue ersetzt ihn und übernimmt die Zahl der Versuche
        eintrag.versuche = platz.naechster.versuche
        platz.naechster.aufloesen({ ok: false, ersetzt: true })
      }
      platz.naechster = eintrag
      if (!platz.laufend && (!platz.timer || this.anmeldung)) {
        // Wartet der Platz auf eine Wiederholung, bleibt es dabei; nur bei abgelaufener Anmeldung sofort probieren
        this.starte(schluessel, platz)
      }
      this.melde()
    })
  }

  /** Alles, was auf eine Wiederholung wartet, sofort senden (wieder online, Seite wieder im Vordergrund) */
  jetztWiederholen(): void {
    for (const [schluessel, platz] of this.plaetze) {
      if (platz.timer && !platz.laufend && platz.naechster) this.starte(schluessel, platz)
    }
  }

  /** Beim Verlassen nachfragen? Nur, wenn ein Stand noch nicht sicher unterwegs ist. */
  mussWarnen(): boolean {
    for (const platz of this.plaetze.values()) {
      if (platz.naechster) return true
      if (platz.laufend && !platz.laufend.keepalive) return true
    }
    return false
  }

  stand(): SpeicherStand {
    let sendet = 0
    let wiederholt = 0
    for (const platz of this.plaetze.values()) {
      for (const e of [platz.laufend, platz.naechster]) {
        if (!e) continue
        if (e.versuche > 0) wiederholt++
        else sendet++
      }
    }
    const fehler = [...this.fehlgeschlagen.values()].pop() || ''
    return {
      sendet,
      wiederholt,
      offline: this.offline,
      anmeldung: this.anmeldung,
      anmeldungZiel: this.anmeldungZiel,
      fehler,
      zuletztGespeichert: this.zuletztGespeichert,
    }
  }

  // ---------- intern ----------

  private melde(): void {
    this.beiAenderung(this.stand())
  }

  private starte(schluessel: string, platz: Platz): void {
    const eintrag = platz.naechster
    if (!eintrag) return
    if (platz.timer) {
      clearTimeout(platz.timer)
      platz.timer = null
    }
    platz.naechster = null
    platz.laufend = eintrag
    void this.uebertrage(schluessel, platz, eintrag)
  }

  private async uebertrage(schluessel: string, platz: Platz, eintrag: Eintrag): Promise<void> {
    const { url, method = 'POST', body } = eintrag.auftrag
    const headers: Record<string, string> = {
      // Aus dem Cookie, nicht aus dem Meta-Tag: nach einer neuen Anmeldung im anderen Tab ist nur das Cookie aktuell
      'X-CSRFToken': csrfTokenAktuell(),
      'X-Requested-With': 'XMLHttpRequest',
      Accept: 'application/json',
    }
    let daten: BodyInit | undefined
    if (body instanceof FormData) {
      daten = body
    } else if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
      daten = JSON.stringify(body)
    }
    eintrag.keepalive = this.verbergen && typeof daten === 'string' && daten.length < KEEPALIVE_GRENZE
    let antwort: Antwortart
    try {
      const resp = await this.abrufen(url, {
        method,
        headers,
        body: daten,
        credentials: 'same-origin',
        keepalive: eintrag.keepalive,
      })
      antwort = await bewerte(resp)
    } catch {
      antwort = { art: 'stoerung' }
    }
    this.verarbeite(schluessel, platz, eintrag, antwort)
  }

  private verarbeite(schluessel: string, platz: Platz, eintrag: Eintrag, antwort: Antwortart): void {
    platz.laufend = null
    const wiederholbar = !!eintrag.auftrag.schluessel

    if (antwort.art === 'ok') {
      this.offline = false
      this.anmeldung = ''
      this.anmeldungZiel = ''
      this.zuletztGespeichert = new Date()
      this.fehlgeschlagen.delete(schluessel)
      // Einmal-Fehler (ohne Schlüssel) gelten wie bisher als erledigt, sobald wieder etwas gespeichert wurde
      for (const k of [...this.fehlgeschlagen.keys()]) if (k.startsWith('einzel-')) this.fehlgeschlagen.delete(k)
      eintrag.aufloesen({ ok: true, daten: antwort.daten })
    } else if (antwort.art === 'fehler' || !wiederholbar) {
      const meldung = this.meldungFuer(eintrag, antwort)
      this.fehlgeschlagen.set(schluessel, meldung)
      if (antwort.art === 'anmeldung') this.setzeAnmeldung(antwort)
      eintrag.aufloesen({ ok: false, ersetzt: false, meldung })
    } else {
      // Störung oder Anmeldung: Stand behalten und später erneut senden (ein neuerer Stand ersetzt ihn)
      eintrag.versuche++
      if (platz.naechster) {
        platz.naechster.versuche = Math.max(platz.naechster.versuche, eintrag.versuche)
        eintrag.aufloesen({ ok: false, ersetzt: true })
      } else {
        platz.naechster = eintrag
      }
      if (antwort.art === 'anmeldung') this.setzeAnmeldung(antwort)
      else this.offline = typeof navigator !== 'undefined' && navigator.onLine === false
      this.plane(schluessel, platz, antwort.art === 'anmeldung')
    }

    if (platz.naechster && !platz.timer) this.starte(schluessel, platz)
    if (!platz.laufend && !platz.naechster && !platz.timer) this.plaetze.delete(schluessel)
    this.melde()
  }

  private plane(schluessel: string, platz: Platz, anmeldung: boolean): void {
    const versuch = platz.naechster?.versuche || 1
    const basis = anmeldung
      ? this.anmeldungWartezeit
      : this.wartezeiten[Math.min(versuch - 1, this.wartezeiten.length - 1)]
    // Etwas Streuung, damit nach einem Neustart nicht alle offenen Seiten im selben Moment senden
    const wartezeit = Math.round(basis * (0.85 + Math.random() * 0.3))
    platz.timer = setTimeout(() => {
      platz.timer = null
      if (platz.naechster && !platz.laufend) this.starte(schluessel, platz)
    }, wartezeit)
  }

  private setzeAnmeldung(antwort: Extract<Antwortart, { art: 'anmeldung' }>): void {
    this.anmeldung = antwort.problem
    this.anmeldungZiel = antwort.ziel
  }

  private meldungFuer(eintrag: Eintrag, antwort: Antwortart): string {
    const was = eintrag.auftrag.bezeichnung || 'Änderung'
    if (antwort.art === 'fehler') return `${was} nicht gespeichert: ${antwort.meldung}`
    if (antwort.art === 'anmeldung') return `${was} nicht gespeichert: Anmeldung abgelaufen`
    return `${was} nicht gespeichert: keine Verbindung zum Server`
  }
}
