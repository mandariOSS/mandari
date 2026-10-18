/**
 * Speichern mit Wiederholung (#854): gemeinsame Speicherlogik der Sitzungsvorbereitung.
 *
 * Bisher ging eine Eingabe verloren, wenn ein Speichern scheiterte (Neustart beim Deploy, kurze Netzstörung):
 * Die Oberfläche zeigte „Nicht gespeichert“, wiederholte aber nichts. Hier gilt:
 *
 * - Je Adresse (Position, Notiz, Redebeitrag eines TOPs …) läuft höchstens eine Anfrage, die übrigen warten in
 *   einer Reihe. So kommen Anfragen an dasselbe Ziel in der Reihenfolge der Eingabe an, und ein älterer Stand
 *   überschreibt nie einen neueren (auch nicht zwischen Feldern wie Position und Begründung oder zwischen
 *   Inhalt und Löschen eines Redebeitrags).
 * - Aktualisierungen (`wiederholbar`) werden mit der direkt folgenden Aktualisierung derselben Adresse
 *   zusammengeführt (neuere Felder gewinnen) und bei Störung (keine Antwort, Zeitüberschreitung,
 *   408/425/429/502/503/504) mit steigender Wartezeit wiederholt, solange die Seite offen ist; 500 bis zu
 *   dreimal (z. B. kurz nach einem Deploy).
 * - Abgelaufene Anmeldung bzw. fehlender zweiter Faktor halten Aktualisierungen an; sie werden erneut gesendet,
 *   sobald die Seite wieder Fokus bekommt, und sonst alle 30 s (Anmeldung in einem anderen Tab genügt).
 * - Alles andere (Anlegen, Löschen, Hochladen, Verknüpfen) wird genau einmal gesendet: Eine Wiederholung könnte
 *   doppelt anlegen. Die Eingabe bleibt beim Aufrufer stehen.
 * - Endgültige Fehler (400, 403, 404 …) werden nicht wiederholt.
 * - Aktualisierungen mit `sicherung` werden zusätzlich im Browser gesichert, bis der Server sie bestätigt hat (Teil 2,
 *   frontend/js/eingaben-sicherung.ts; in den localStorage erst bei Störung, ausbleibender Bestätigung oder beim
 *   Verlassen der Seite): Sie überstehen so auch ein Neuladen oder Schließen der Seite.
 *
 * Das Modul kennt kein Alpine; der Stand wird über `beiAenderung` gemeldet.
 */

import { csrfTokenAktuell } from './csrf'
import type { Sicherung, SicherungsZiel } from './eingaben-sicherung'

// biome-ignore lint/suspicious/noExplicitAny: Antworten der JSON-Endpunkte sind nicht schematisiert
export type JsonAntwort = Record<string, any>

export interface SpeicherAuftrag {
  url: string
  method?: string
  /** JSON-Objekt oder FormData */
  body?: unknown
  /** Aktualisierung von Feldern (idempotent): zusammenführen und bei Störung wiederholen */
  wiederholbar?: boolean
  /**
   * Für Meldungen, z. B. „Position zu TOP 2“; als Funktion aus den tatsächlich gesendeten Feldern gebildet
   * (zusammengeführte Aktualisierungen nennen dann alle Felder)
   */
  bezeichnung?: string | ((body: Record<string, unknown>) => string)
  /**
   * Nur Aktualisierungen: Felder bis zur Bestätigung durch den Server im Browser sichern (TOP und Bereich).
   * Gleiche Adresse heißt gleiches Ziel; zusammengeführte Aufträge behalten es.
   */
  sicherung?: SicherungsZiel
}

export type SpeicherErgebnis =
  | { ok: true; daten: JsonAntwort }
  | { ok: false; ersetzt: true }
  | { ok: false; ersetzt: false; meldung: string }

export type Anmeldeproblem = '' | 'abgelaufen' | 'zweiter_faktor'

export interface SpeicherStand {
  /** Anfragen unterwegs bzw. eingereiht */
  sendet: number
  /** Aktualisierungen, die nach einer Störung noch nicht gespeichert sind (warten oder werden gerade wiederholt) */
  wiederholt: number
  offline: boolean
  anmeldung: Anmeldeproblem
  /** Adresse zum Einrichten des zweiten Faktors (aus der Antwort des Servers) */
  anmeldungZiel: string
  /** Zuletzt endgültig gescheitert (nicht wiederholbar); leer, wenn nichts offen ist */
  fehler: string
  zuletztGespeichert: Date | null
}

export interface SpeicherOptionen {
  beiAenderung?: (stand: SpeicherStand) => void
  /** Wartezeiten zwischen den Versuchen; der letzte Wert gilt für alle weiteren */
  wartezeitenMs?: number[]
  /** Abstand der Versuche bei abgelaufener Anmeldung */
  anmeldungWartezeitMs?: number
  /** Zeitlimit je Anfrage einer Aktualisierung (danach gilt sie als gestört und wird wiederholt) */
  zeitlimitMs?: number
  fetch?: typeof fetch
  /** Sicherung der Eingaben im Browser (frontend/js/eingaben-sicherung.ts) */
  sicherung?: Sicherung
}

const WARTEZEITEN_MS = [1000, 2000, 4000, 8000, 15000, 30000]
const ANMELDUNG_WARTEZEIT_MS = 30000
const ZEITLIMIT_MS = 30000
/** Antworten, bei denen die Anfrage nicht (sicher) verarbeitet wurde und eine Wiederholung hilft */
const VORUEBERGEHEND = new Set([408, 425, 429, 502, 503, 504])
/** Versuche bei 500 (Serverfehler, z. B. direkt nach einem Deploy), danach endgültig */
const VERSUCHE_BEI_500 = 3
/** Browser begrenzen alle gleichzeitig laufenden keepalive-Anfragen zusammen auf 64 KiB */
const KEEPALIVE_GRENZE = 60000

interface Eintrag {
  auftrag: SpeicherAuftrag
  versuche: number
  /** davon mit Antwort 500 (eigene Grenze, Störungen zählen nicht mit) */
  serverfehler: number
  /** Bytes, die gerade mit keepalive unterwegs sind (überleben das Verlassen der Seite); 0 = ohne keepalive */
  keepalive: number
  aufloesen: (ergebnis: SpeicherErgebnis) => void
}

/** Endgültig gescheitert: Meldung und – bei Aktualisierungen – die Felder, die noch nicht gespeichert sind */
interface Fehlschlag {
  meldung: string
  felder: Set<string>
}

interface Platz {
  laufend: Eintrag | null
  schlange: Eintrag[]
  timer: ReturnType<typeof setTimeout> | null
}

type Antwortart =
  | { art: 'ok'; daten: JsonAntwort }
  | { art: 'stoerung' }
  | { art: 'serverfehler'; status: number }
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
  if (VORUEBERGEHEND.has(resp.status)) return { art: 'stoerung' }
  if (resp.ok) {
    const daten = await leseJson(resp)
    return daten ? { art: 'ok', daten } : { art: 'fehler', meldung: 'unerwartete Antwort des Servers' }
  }
  const daten = istJson(resp) ? await leseJson(resp) : null
  if (resp.status === 403 && daten?.error === 'two_factor_setup_required') {
    return { art: 'anmeldung', problem: 'zweiter_faktor', ziel: String(daten.redirect || '') }
  }
  if (resp.status === 500) return { art: 'serverfehler', status: 500 }
  if (typeof daten?.error === 'string') return { art: 'fehler', meldung: daten.error }
  return { art: 'fehler', meldung: resp.status === 403 ? 'Zugriff verweigert' : `Fehler ${resp.status}` }
}

function istObjekt(wert: unknown): wert is Record<string, unknown> {
  return typeof wert === 'object' && wert !== null && !(wert instanceof FormData) && !Array.isArray(wert)
}

/** Zwei Aktualisierungen derselben Adresse lassen sich zu einer zusammenführen (die spätere gewinnt je Feld) */
function zusammenfuehrbar(a: Eintrag, b: Eintrag): boolean {
  return (
    !!a.auftrag.wiederholbar &&
    !!b.auftrag.wiederholbar &&
    (a.auftrag.method || 'POST') === (b.auftrag.method || 'POST') &&
    istObjekt(a.auftrag.body) &&
    istObjekt(b.auftrag.body)
  )
}

function bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

export class Speicherdienst {
  /** Seite wird verborgen bzw. verlassen: kleine Anfragen mit keepalive senden */
  verbergen = false

  /** Je Adresse eine Reihe */
  private plaetze = new Map<string, Platz>()
  /** Endgültig gescheiterte Aufträge (Aktualisierungen je Adresse, sonst je Auftrag) */
  private fehlgeschlagen = new Map<string, Fehlschlag>()
  private zaehler = 0
  private keepaliveBytes = 0
  private anmeldung: Anmeldeproblem = ''
  private anmeldungZiel = ''
  private offline = false
  private zuletztGespeichert: Date | null = null
  private readonly beiAenderung: (stand: SpeicherStand) => void
  private readonly wartezeiten: number[]
  private readonly anmeldungWartezeit: number
  private readonly zeitlimit: number
  private readonly abrufen: typeof fetch
  private readonly sicherung: Sicherung | null

  constructor(optionen: SpeicherOptionen = {}) {
    this.beiAenderung = optionen.beiAenderung || (() => {})
    this.wartezeiten = optionen.wartezeitenMs?.length ? optionen.wartezeitenMs : WARTEZEITEN_MS
    this.anmeldungWartezeit = optionen.anmeldungWartezeitMs ?? ANMELDUNG_WARTEZEIT_MS
    this.zeitlimit = optionen.zeitlimitMs ?? ZEITLIMIT_MS
    this.abrufen = optionen.fetch || ((...args: Parameters<typeof fetch>) => fetch(...args))
    this.sicherung = optionen.sicherung || null
  }

  /**
   * Auftrag senden. Die Anfrage startet synchron (wichtig beim Verlassen der Seite), außer an dieselbe Adresse
   * läuft schon eine bzw. es wird auf eine Wiederholung gewartet; dann reiht sie sich ein. Das Ergebnis kommt,
   * wenn gespeichert, endgültig gescheitert oder durch einen späteren Stand ersetzt (zusammengeführt).
   */
  senden(auftrag: SpeicherAuftrag): Promise<SpeicherErgebnis> {
    // Zuerst sichern (im Speicher der Seite; beim Verlassen schreibt die Vorbereitung es fest)
    if (auftrag.wiederholbar && auftrag.sicherung && istObjekt(auftrag.body)) {
      this.sicherung?.merken(auftrag.sicherung, auftrag.body)
    }
    return new Promise<SpeicherErgebnis>((aufloesen) => {
      let platz = this.plaetze.get(auftrag.url)
      if (!platz) {
        platz = { laufend: null, schlange: [], timer: null }
        this.plaetze.set(auftrag.url, platz)
      }
      const eintrag: Eintrag = { auftrag, versuche: 0, serverfehler: 0, keepalive: 0, aufloesen }
      const letzter = platz.schlange[platz.schlange.length - 1]
      if (letzter && zusammenfuehrbar(letzter, eintrag)) {
        platz.schlange[platz.schlange.length - 1] = this.zusammenfuehren(letzter, eintrag)
      } else {
        platz.schlange.push(eintrag)
      }
      // Wartet die Adresse auf eine Wiederholung, bleibt es dabei; nur bei abgelaufener Anmeldung sofort probieren
      if (!platz.laufend && (!platz.timer || this.anmeldung)) this.starte(platz)
      this.melde()
    })
  }

  /** Alles, was auf eine Wiederholung wartet, sofort senden (wieder online, Seite wieder im Vordergrund) */
  jetztWiederholen(): void {
    for (const platz of this.plaetze.values()) {
      if (platz.timer && !platz.laufend) this.starte(platz)
    }
  }

  /** Beim Verlassen nachfragen? Wenn eine Eingabe noch nicht sicher unterwegs bzw. endgültig nicht gespeichert ist. */
  mussWarnen(): boolean {
    for (const platz of this.plaetze.values()) {
      if (platz.schlange.length > 0) return true
      if (platz.laufend && !platz.laufend.keepalive) return true
    }
    for (const schluessel of this.fehlgeschlagen.keys()) {
      if (schluessel.startsWith('stand:')) return true
    }
    return false
  }

  stand(): SpeicherStand {
    let sendet = 0
    let wiederholt = 0
    for (const platz of this.plaetze.values()) {
      for (const e of [platz.laufend, ...platz.schlange]) {
        if (!e) continue
        if (e.versuche > 0) wiederholt++
        else sendet++
      }
    }
    return {
      sendet,
      wiederholt,
      offline: this.offline,
      anmeldung: this.anmeldung,
      anmeldungZiel: this.anmeldungZiel,
      fehler: [...this.fehlgeschlagen.values()].pop()?.meldung || '',
      zuletztGespeichert: this.zuletztGespeichert,
    }
  }

  // ---------- intern ----------

  private melde(): void {
    this.beiAenderung(this.stand())
  }

  /** `spaeter` ersetzt `frueher`: Felder zusammengeführt, die Zahl der Versuche bleibt erhalten */
  private zusammenfuehren(frueher: Eintrag, spaeter: Eintrag): Eintrag {
    frueher.aufloesen({ ok: false, ersetzt: true })
    return {
      auftrag: {
        ...spaeter.auftrag,
        body: { ...(frueher.auftrag.body as object), ...(spaeter.auftrag.body as object) },
      },
      versuche: Math.max(frueher.versuche, spaeter.versuche),
      serverfehler: Math.max(frueher.serverfehler, spaeter.serverfehler),
      keepalive: 0,
      aufloesen: spaeter.aufloesen,
    }
  }

  private starte(platz: Platz): void {
    if (platz.timer) {
      clearTimeout(platz.timer)
      platz.timer = null
    }
    const eintrag = platz.schlange.shift()
    if (!eintrag) return
    platz.laufend = eintrag
    void this.uebertrage(platz, eintrag)
  }

  private async uebertrage(platz: Platz, eintrag: Eintrag): Promise<void> {
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
    if (this.verbergen && typeof daten === 'string') {
      const groesse = bytes(daten)
      if (this.keepaliveBytes + groesse < KEEPALIVE_GRENZE) {
        eintrag.keepalive = groesse
        this.keepaliveBytes += groesse
      }
    }
    // Zeitlimit für alles außer Uploads (die dürfen bei langsamer Verbindung dauern): eine hängende Anfrage
    // hielte sonst die ganze Reihe ihrer Adresse auf
    // Einmal-Aufträge bekommen mehr Zeit: Bricht ein Anlegen ab, das der Server doch noch ausführt, entstünde beim
    // erneuten Klick ein doppelter Eintrag
    const zeitlimit =
      !(body instanceof FormData) && typeof AbortSignal.timeout === 'function'
        ? AbortSignal.timeout(eintrag.auftrag.wiederholbar ? this.zeitlimit : this.zeitlimit * 3)
        : null
    let antwort: Antwortart
    try {
      const resp = await this.abrufen(url, {
        method,
        headers,
        body: daten,
        credentials: 'same-origin',
        keepalive: eintrag.keepalive > 0,
        ...(zeitlimit ? { signal: zeitlimit } : {}),
      })
      antwort = await bewerte(resp)
      // Zeitlimit lief ab, während die Antwort gelesen wurde: Störung, kein endgültiger Fehler
      if (antwort.art === 'fehler' && zeitlimit?.aborted) antwort = { art: 'stoerung' }
    } catch {
      antwort = { art: 'stoerung' }
    }
    this.keepaliveBytes -= eintrag.keepalive
    eintrag.keepalive = 0
    this.verarbeite(platz, eintrag, antwort)
  }

  private verarbeite(platz: Platz, eintrag: Eintrag, antwort: Antwortart): void {
    platz.laufend = null
    const { url, wiederholbar, body, method = 'POST', sicherung } = eintrag.auftrag

    if (antwort.art === 'ok') {
      this.offline = false
      this.zuletztGespeichert = new Date()
      this.erledige(url, method, body)
      // Gespeichert: Sicherung im Browser für genau diese Werte aufgeben (zusammengeführte Felder eingeschlossen)
      if (wiederholbar && sicherung && istObjekt(body)) this.sicherung?.gespeichert(sicherung, body)
      // Einmal-Fehler gelten wie bisher als erledigt, sobald wieder etwas gespeichert wurde
      for (const k of [...this.fehlgeschlagen.keys()]) {
        if (k.startsWith('einzel:')) this.fehlgeschlagen.delete(k)
      }
      eintrag.aufloesen({ ok: true, daten: antwort.daten })
      if (this.anmeldung) {
        // Wieder angemeldet: alles, was auf die Anmeldung wartet, jetzt senden
        this.anmeldung = ''
        this.anmeldungZiel = ''
        this.jetztWiederholen()
      }
    } else if (wiederholbar && this.nochmal(eintrag, antwort)) {
      // Stand behalten und später erneut senden; eine direkt folgende Aktualisierung wird angehängt
      eintrag.versuche++
      if (antwort.art === 'serverfehler') eintrag.serverfehler++
      const naechster = platz.schlange[0]
      if (naechster && zusammenfuehrbar(eintrag, naechster)) {
        platz.schlange[0] = this.zusammenfuehren(eintrag, naechster)
      } else {
        platz.schlange.unshift(eintrag)
      }
      if (antwort.art === 'anmeldung') {
        this.anmeldung = antwort.problem
        this.anmeldungZiel = antwort.ziel
      } else {
        this.offline = typeof navigator !== 'undefined' && navigator.onLine === false
      }
      this.plane(platz, antwort.art === 'anmeldung')
    } else {
      const meldung = this.meldungFuer(eintrag, antwort)
      const schluessel = wiederholbar ? `stand:${url}` : `einzel:${++this.zaehler}`
      const felder = new Set(this.fehlgeschlagen.get(schluessel)?.felder)
      if (wiederholbar && istObjekt(body)) for (const feld of Object.keys(body)) felder.add(feld)
      // Neu einsortieren, damit die jüngste Meldung angezeigt wird
      this.fehlgeschlagen.delete(schluessel)
      this.fehlgeschlagen.set(schluessel, { meldung, felder })
      eintrag.aufloesen({ ok: false, ersetzt: false, meldung })
    }

    if (platz.schlange.length > 0 && !platz.timer && !platz.laufend) this.starte(platz)
    if (!platz.laufend && platz.schlange.length === 0 && !platz.timer) this.plaetze.delete(url)
    this.melde()
  }

  private nochmal(eintrag: Eintrag, antwort: Antwortart): boolean {
    if (antwort.art === 'stoerung' || antwort.art === 'anmeldung') return true
    return antwort.art === 'serverfehler' && eintrag.serverfehler + 1 < VERSUCHE_BEI_500
  }

  /**
   * Endgültigen Fehler einer Adresse erst löschen, wenn alle damals gescheiterten Felder gespeichert sind
   * (eine gespeicherte Position räumt nicht die verlorene Begründung weg) bzw. das Ziel gelöscht wurde.
   */
  private erledige(url: string, method: string, body: unknown): void {
    const schluessel = `stand:${url}`
    const offen = this.fehlgeschlagen.get(schluessel)
    if (!offen) return
    if (method === 'DELETE') {
      this.fehlgeschlagen.delete(schluessel)
      return
    }
    if (!istObjekt(body)) return
    for (const feld of Object.keys(body)) offen.felder.delete(feld)
    if (offen.felder.size === 0) this.fehlgeschlagen.delete(schluessel)
  }

  private plane(platz: Platz, anmeldung: boolean): void {
    const versuch = platz.schlange[0]?.versuche || 1
    const basis = anmeldung
      ? this.anmeldungWartezeit
      : this.wartezeiten[Math.min(versuch - 1, this.wartezeiten.length - 1)]
    // Etwas Streuung, damit nach einem Neustart nicht alle offenen Seiten im selben Moment senden
    const wartezeit = Math.round(basis * (0.85 + Math.random() * 0.3))
    platz.timer = setTimeout(() => {
      platz.timer = null
      if (!platz.laufend) this.starte(platz)
    }, wartezeit)
  }

  private meldungFuer(eintrag: Eintrag, antwort: Antwortart): string {
    const { bezeichnung, body } = eintrag.auftrag
    const was =
      (typeof bezeichnung === 'function' ? (istObjekt(body) ? bezeichnung(body) : '') : bezeichnung) || 'Änderung'
    if (antwort.art === 'fehler') return `${was} nicht gespeichert: ${antwort.meldung}`
    if (antwort.art === 'serverfehler') return `${was} nicht gespeichert: Fehler auf dem Server`
    if (antwort.art === 'anmeldung') return `${was} nicht gespeichert: Anmeldung abgelaufen`
    return `${was} nicht gespeichert: keine Verbindung zum Server`
  }
}
