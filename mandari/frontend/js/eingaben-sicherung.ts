/**
 * Sicherung ungespeicherter Eingaben im Browser (#854, Teil 2).
 *
 * Was die Sitzungsvorbereitung an Feldern speichert (Position, Begründung, Notiz, Redebeitrag, Sitzungsnotizen),
 * wird zusätzlich gesichert, bis der Server genau diesen Stand bestätigt hat. So übersteht eine Eingabe ein
 * Neuladen, das Schließen der Seite oder einen Absturz des Browsers, solange ihr Speichern gestört oder noch offen
 * ist; beim nächsten Aufruf bietet die Vorbereitung sie zur Übernahme an.
 *
 * - Schlüssel je Konto, Organisation, Sitzung und TOP: `mandari.eingaben.<Konto>.<Organisation>.<Sitzung>.<TOP>`.
 *   Position, Begründung und Notizen gelten je Organisation; dieselbe Ratssitzung wird in jeder Organisation getrennt
 *   vorbereitet. Im Schlüssel stehen Kennungen, nie Namen; ohne gültige Kennungen wird nichts gesichert.
 * - Klartext so selten wie möglich auf der Platte: Eine Eingabe liegt zunächst nur im Speicher der Seite. In den
 *   localStorage kommt sie erst, wenn der Server sie nicht binnen `VERZOEGERUNG_MS` bestätigt (Störung, Wiederholung,
 *   abgelaufene Anmeldung, Ablehnung) oder wenn die Seite verborgen bzw. verlassen wird (`festschreiben`). Bestätigt
 *   der Server sofort, wird nichts geschrieben.
 * - Ein Feld verschwindet, sobald der Server genau diesen Wert bestätigt hat (eine neuere Eingabe bleibt bis zu
 *   ihrer eigenen Bestätigung), ein leerer Eintrag ganz.
 * - Je Feld steht dabei der Stand des Servers, auf dem die Eingabe beruht (`vorher`). Hat der Server beim nächsten
 *   Aufruf einen anderen Stand, wurde das Feld inzwischen anderweitig geändert (`vergleichen`).
 * - Je Feld höchstens 24 Stunden nach der Eingabe: Älteres wird beim Lesen und bei jedem Seitenaufruf von mandari
 *   gelöscht (`aufraeumen`, frontend/js/main.ts), auf angemeldeten Seiten dabei auch die Sicherungen anderer Konten.
 *   Ohne weiteren Aufruf bleibt ein festgeschriebener Eintrag bis dahin im Browserprofil.
 * - Beim Abmelden werden alle Sicherungen gelöscht (`alleLoeschen`, Seite „Abgemeldet“); noch offene Seiten der
 *   Vorbereitung sichern danach nichts mehr (`beiAbmeldungSperren`).
 * - Ohne localStorage (privater Modus, Speicher voll) läuft alles ohne Sicherung weiter.
 *
 * Das Modul kennt weder Alpine noch den Speicherdienst; der Speicherdienst ruft `merken` und `gespeichert` über die
 * Schnittstelle `Sicherung` auf (frontend/js/speichern.ts).
 */

export const PRAEFIX = 'mandari.eingaben.'
/** Markierung der Seite „Abgemeldet“ für noch offene Seiten (Wert: Zeitpunkt); kein Eintrag mit `PRAEFIX` */
export const ABGEMELDET = 'mandari.eingaben-abgemeldet'
/** Höchstens so lange nach der Eingabe bleibt ein gesichertes Feld erhalten */
export const HOECHSTDAUER_MS = 24 * 60 * 60 * 1000
/** So lange darf die Bestätigung des Servers dauern, bevor eine Eingabe in den localStorage geschrieben wird */
export const VERZOEGERUNG_MS = 1500

export type Wert = string | number | boolean
export type Felder = Record<string, Wert>

/** Wohin eine Aktualisierung in der Sicherung gehört: TOP (bzw. ein fester Name) und Bereich (z. B. „notiz“) */
export interface SicherungsZiel {
  top: string
  bereich: string
}

export interface GesichertesFeld {
  /** Die Eingabe */
  wert: Wert
  /** Stand des Servers, auf dem die Eingabe beruht (beim Laden bzw. zuletzt bestätigt); fehlt, wenn unbekannt */
  vorher?: Wert
  /** Zeitpunkt der Eingabe (Millisekunden seit 1970) */
  zeit: number
}

export type GesicherterBereich = Record<string, GesichertesFeld>

export interface GesicherterEintrag {
  /** Felder je Bereich, z. B. `{ position: { reasoning: { wert: '…', vorher: '', zeit: … } } }` */
  bereiche: Record<string, GesicherterBereich>
}

/** Was der Speicherdienst von der Sicherung braucht */
export interface Sicherung {
  /** Eingabe vor dem Senden festhalten */
  merken(ziel: SicherungsZiel, felder: Record<string, unknown>): void
  /** Der Server hat diese Felder mit genau diesen Werten gespeichert */
  gespeichert(ziel: SicherungsZiel, felder: Record<string, unknown>): void
}

export interface SicherungsOptionen {
  speicher?: Storage | null
  jetzt?: () => number
  /** Wartezeit bis zum Schreiben in den localStorage (Vorgabe `VERZOEGERUNG_MS`) */
  verzoegerungMs?: number
}

function standardSpeicher(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    // Zugriff verweigert (z. B. Cookies und Websitedaten blockiert)
    return null
  }
}

function istWert(wert: unknown): wert is Wert {
  return typeof wert === 'string' || typeof wert === 'number' || typeof wert === 'boolean'
}

function istObjekt(wert: unknown): wert is Record<string, unknown> {
  return typeof wert === 'object' && wert !== null && !Array.isArray(wert)
}

/** Eigene Eigenschaft (nie geerbte wie `toString`) */
function eigenes(objekt: object, name: string): boolean {
  return Object.getOwnPropertyDescriptor(objekt, name) !== undefined
}

const GUELTIGE_KENNUNG = /^[\w-]+$/

/** Gleicher Stand? Leerzeichen am Rand zählen bei Texten nicht. */
export function gleicherStand(a: unknown, b: unknown): boolean {
  if (typeof a === 'string' && typeof b === 'string') return a.trim() === b.trim()
  return a === b
}

function schluesselMit(speicher: Storage, praefix: string): string[] {
  const liste: string[] = []
  try {
    for (let i = 0; i < speicher.length; i++) {
      const schluessel = speicher.key(i)
      if (schluessel?.startsWith(praefix)) liste.push(schluessel)
    }
  } catch {
    /* ohne Zugriff: nichts */
  }
  return liste
}

function entferne(speicher: Storage, schluessel: string): void {
  try {
    speicher.removeItem(schluessel)
  } catch {
    /* ohne Zugriff: nichts */
  }
}

function abgelaufen(zeit: number, jetzt: number): boolean {
  return jetzt - zeit > HOECHSTDAUER_MS
}

function feldAus(roh: unknown): GesichertesFeld | null {
  if (!istObjekt(roh)) return null
  const { wert, vorher, zeit } = roh
  if (!istWert(wert) || typeof zeit !== 'number' || !Number.isFinite(zeit)) return null
  return istWert(vorher) ? { wert, vorher, zeit } : { wert, zeit }
}

/** Eintrag ohne Unlesbares und Abgelaufenes (je Feld); `null`, wenn nichts bleibt */
function bereinigt(roh: unknown, jetzt: number): GesicherterEintrag | null {
  if (!istObjekt(roh) || !istObjekt(roh.bereiche)) return null
  const bereiche: Array<[string, GesicherterBereich]> = []
  for (const [bereich, felderRoh] of Object.entries(roh.bereiche)) {
    if (!istObjekt(felderRoh)) continue
    const felder: Array<[string, GesichertesFeld]> = []
    for (const [name, feldRoh] of Object.entries(felderRoh)) {
      const feld = feldAus(feldRoh)
      if (feld && !abgelaufen(feld.zeit, jetzt)) felder.push([name, feld])
    }
    // fromEntries legt eigene Eigenschaften an (auch für Namen wie „__proto__“)
    if (felder.length > 0) bereiche.push([bereich, Object.fromEntries(felder)])
  }
  return bereiche.length > 0 ? { bereiche: Object.fromEntries(bereiche) } : null
}

/**
 * Gesicherten Eintrag aus dem localStorage lesen; Abgelaufenes wird dabei gelöscht, ein Eintrag ohne gültige Felder
 * ganz.
 */
function pruefe(speicher: Storage, schluessel: string, jetzt: number): GesicherterEintrag | null {
  let roh: string | null
  try {
    roh = speicher.getItem(schluessel)
  } catch {
    return null
  }
  if (roh === null) return null
  let daten: unknown = null
  try {
    daten = JSON.parse(roh)
  } catch {
    /* unlesbar: weg damit */
  }
  const eintrag = bereinigt(daten, jetzt)
  if (!eintrag) {
    entferne(speicher, schluessel)
  } else if (JSON.stringify(eintrag) !== roh) {
    // Gekürzt (abgelaufene Felder): ohne erneutes Aufräumen schreiben, sonst liefe ein voller Speicher im Kreis
    try {
      speicher.setItem(schluessel, JSON.stringify(eintrag))
    } catch {
      /* bleibt bis zum nächsten Lesen, abgelaufene Felder werden dann wieder übergangen */
    }
  }
  return eintrag
}

function schreibe(speicher: Storage, schluessel: string, eintrag: GesicherterEintrag, jetzt: number): void {
  try {
    speicher.setItem(schluessel, JSON.stringify(eintrag))
  } catch {
    // Speicher voll: Abgelaufenes räumen und einmal erneut versuchen, sonst ohne Sicherung weiter
    aufraeumen(speicher, jetzt)
    try {
      speicher.setItem(schluessel, JSON.stringify(eintrag))
    } catch {
      /* ohne Sicherung weiter */
    }
  }
}

function kopie(eintrag: GesicherterEintrag): GesicherterEintrag {
  return JSON.parse(JSON.stringify(eintrag)) as GesicherterEintrag
}

export class Eingabensicherung implements Sicherung {
  /** `mandari.eingaben.<Konto>.<Organisation>.<Sitzung>.`; leer, wenn nicht gesichert werden darf */
  private readonly basis: string
  private readonly speicher: Storage | null
  private readonly jetzt: () => number
  private readonly verzoegerung: number
  /** Bekannter Stand des Servers je Ziel (beim Laden, nach Bestätigung, aus Echtzeit): Grundlage neuer Eingaben */
  private readonly serverstand = new Map<string, Map<string, Wert>>()
  /** Eingaben, die (noch) nicht im localStorage liegen, mit dem Zeitgeber fürs Schreiben */
  private readonly vorgemerkt = new Map<
    string,
    { eintrag: GesicherterEintrag; zeitgeber: ReturnType<typeof setTimeout> }
  >()
  private gesperrt = false

  constructor(konto: string, organisation: string, sitzung: string, optionen: SicherungsOptionen = {}) {
    // Punkte trennen die Teile des Schlüssels; Kennungen mit Punkt würden fremde Einträge treffen
    const gueltig = [konto, organisation, sitzung].every((teil) => GUELTIGE_KENNUNG.test(teil))
    this.basis = gueltig ? `${PRAEFIX}${konto}.${organisation}.${sitzung}.` : ''
    this.speicher = optionen.speicher === undefined ? standardSpeicher() : optionen.speicher
    this.jetzt = optionen.jetzt || Date.now
    this.verzoegerung = optionen.verzoegerungMs ?? VERZOEGERUNG_MS
  }

  get aktiv(): boolean {
    return !!this.basis && !!this.speicher && !this.gesperrt
  }

  /** Stand des Servers (beim Laden, aus Echtzeit): Grundlage der folgenden Eingaben in diesen Feldern */
  serverstandSetzen(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    const ort = `${ziel.top}\n${ziel.bereich}`
    const stand = this.serverstand.get(ort) || new Map<string, Wert>()
    this.serverstand.set(ort, stand)
    for (const [feld, wert] of Object.entries(felder)) {
      if (istWert(wert)) stand.set(feld, wert)
      else stand.delete(feld)
    }
  }

  merken(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    const werte = Object.entries(felder).filter((paar): paar is [string, Wert] => istWert(paar[1]))
    if (!this.aktiv || werte.length === 0) return
    const schluessel = this.basis + ziel.top
    const eintrag = this.eintrag(schluessel) || { bereiche: {} }
    const stand = this.serverstand.get(`${ziel.top}\n${ziel.bereich}`)
    const zeit = this.jetzt()
    const bereich: GesicherterBereich = eigenes(eintrag.bereiche, ziel.bereich) ? eintrag.bereiche[ziel.bereich] : {}
    for (const [feld, wert] of werte) {
      const vorher = stand?.get(feld)
      bereich[feld] = vorher === undefined ? { wert, zeit } : { wert, vorher, zeit }
    }
    eintrag.bereiche[ziel.bereich] = bereich
    this.ablegen(schluessel, eintrag)
  }

  gespeichert(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    // Der Server hat jetzt diesen Stand: Grundlage weiterer Eingaben
    this.serverstandSetzen(ziel, felder)
    this.aendern(ziel, (bereich) => {
      for (const [feld, wert] of Object.entries(felder)) {
        if (!istWert(wert) || !eigenes(bereich, feld)) continue
        if (bereich[feld].wert === wert) delete bereich[feld]
        // Eine neuere Eingabe beruht nun auf dem bestätigten Stand (kein Konflikt mit der eigenen Eingabe)
        else bereich[feld].vorher = wert
      }
    })
  }

  /** Diese Felder vergessen, sofern sie noch genau diese Werte haben (verworfen oder schon auf dem Server) */
  vergessen(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    this.aendern(ziel, (bereich) => {
      for (const [feld, wert] of Object.entries(felder)) {
        // Nur, was genau so gesichert ist; eine neuere Eingabe bleibt
        if (istWert(wert) && eigenes(bereich, feld) && bereich[feld].wert === wert) delete bereich[feld]
      }
    })
  }

  /** Ganzen Bereich vergessen, z. B. nach dem Löschen des Redebeitrags */
  verwerfen(ziel: SicherungsZiel): void {
    this.aendern(ziel, (bereich) => {
      for (const feld of Object.keys(bereich)) delete bereich[feld]
    })
  }

  /** Alles zu einem TOP vergessen */
  entfernen(top: string): void {
    if (!this.basis) return
    const schluessel = this.basis + top
    this.nichtVormerken(schluessel)
    if (this.speicher) entferne(this.speicher, schluessel)
  }

  /** Gesicherte Eingaben zu einem TOP (abgelaufene werden dabei gelöscht) */
  lesen(top: string): GesicherterEintrag | null {
    if (!this.basis) return null
    return this.eintrag(this.basis + top)
  }

  /** Alle gesicherten Eingaben dieses Kontos zu dieser Sitzung (abgelaufene werden dabei gelöscht) */
  alle(): Array<{ top: string; eintrag: GesicherterEintrag }> {
    if (!this.basis) return []
    const schluessel = new Set(this.vorgemerkt.keys())
    if (this.speicher) for (const s of schluesselMit(this.speicher, this.basis)) schluessel.add(s)
    const liste: Array<{ top: string; eintrag: GesicherterEintrag }> = []
    for (const s of schluessel) {
      const eintrag = this.eintrag(s)
      if (eintrag) liste.push({ top: s.slice(this.basis.length), eintrag })
    }
    return liste
  }

  /** Noch nicht bestätigte Eingaben sofort in den localStorage schreiben (Seite wird verborgen oder verlassen) */
  festschreiben(): void {
    for (const schluessel of [...this.vorgemerkt.keys()]) this.festschreibenFuer(schluessel)
  }

  /** Nach dem Abmelden (in einem anderen Tab): nichts mehr sichern, noch nicht Geschriebenes verwerfen */
  sperren(): void {
    this.gesperrt = true
    for (const schluessel of [...this.vorgemerkt.keys()]) this.nichtVormerken(schluessel)
  }

  /** Aktueller Eintrag (vorgemerkt oder aus dem localStorage), als Kopie */
  private eintrag(schluessel: string): GesicherterEintrag | null {
    const vorgemerkt = this.vorgemerkt.get(schluessel)
    if (vorgemerkt) {
      const eintrag = bereinigt(vorgemerkt.eintrag, this.jetzt())
      if (!eintrag) this.nichtVormerken(schluessel)
      return eintrag ? kopie(eintrag) : null
    }
    return this.speicher ? pruefe(this.speicher, schluessel, this.jetzt()) : null
  }

  /** Geänderten Eintrag ablegen: im localStorage, wenn er dort schon liegt, sonst erst einmal vorgemerkt */
  private ablegen(schluessel: string, eintrag: GesicherterEintrag): void {
    if (Object.keys(eintrag.bereiche).length === 0) {
      this.nichtVormerken(schluessel)
      if (this.speicher) entferne(this.speicher, schluessel)
      return
    }
    const vorgemerkt = this.vorgemerkt.get(schluessel)
    if (vorgemerkt) {
      vorgemerkt.eintrag = eintrag
    } else if (this.liegtImSpeicher(schluessel)) {
      // Schon geschrieben (gestört oder aus einem früheren Aufruf): aktuell halten
      if (this.speicher && !this.gesperrt) schreibe(this.speicher, schluessel, eintrag, this.jetzt())
    } else if (this.aktiv) {
      const zeitgeber = setTimeout(() => this.festschreibenFuer(schluessel), this.verzoegerung)
      this.vorgemerkt.set(schluessel, { eintrag, zeitgeber })
    }
  }

  private aendern(ziel: SicherungsZiel, aenderung: (bereich: GesicherterBereich) => void): void {
    if (!this.basis) return
    const schluessel = this.basis + ziel.top
    const eintrag = this.eintrag(schluessel)
    if (!eintrag || !eigenes(eintrag.bereiche, ziel.bereich)) return
    aenderung(eintrag.bereiche[ziel.bereich])
    if (Object.keys(eintrag.bereiche[ziel.bereich]).length === 0) delete eintrag.bereiche[ziel.bereich]
    this.ablegen(schluessel, eintrag)
  }

  private festschreibenFuer(schluessel: string): void {
    const vorgemerkt = this.vorgemerkt.get(schluessel)
    if (!vorgemerkt) return
    this.nichtVormerken(schluessel)
    if (this.speicher && !this.gesperrt) schreibe(this.speicher, schluessel, vorgemerkt.eintrag, this.jetzt())
  }

  private nichtVormerken(schluessel: string): void {
    const vorgemerkt = this.vorgemerkt.get(schluessel)
    if (!vorgemerkt) return
    clearTimeout(vorgemerkt.zeitgeber)
    this.vorgemerkt.delete(schluessel)
  }

  private liegtImSpeicher(schluessel: string): boolean {
    try {
      return !!this.speicher && this.speicher.getItem(schluessel) !== null
    } catch {
      return false
    }
  }
}

/** Ein gesichertes Feld, zur Übernahme angeboten */
export interface AngebotenesFeld {
  wert: Wert
  /** Wert auf der Seite, als das Angebot entstand (beim Laden: Stand des Servers) */
  server: unknown
  /** Der Server hat sich seit der Eingabe anderweitig geändert; Übernehmen ersetzt diesen Stand */
  konflikt: boolean
  /** Zeitpunkt der Eingabe */
  zeit: number
}

/**
 * Gesicherte Felder eines Bereichs mit dem Stand des Servers vergleichen. Was der Server schon hat und was nicht
 * (mehr) passt (`serverWert` liefert `undefined`), ist erledigt; der Rest wird angeboten. Ein Konflikt ist es, wenn
 * der Server weder die Eingabe noch den Stand hat, auf dem sie beruht (inzwischen von anderer Seite oder auf einem
 * anderen Gerät geändert).
 */
export function vergleichen(
  bereich: GesicherterBereich,
  serverWert: (feld: string) => unknown,
): { erledigt: Felder; angebot: Record<string, AngebotenesFeld> } {
  const erledigt: Array<[string, Wert]> = []
  const angebot: Array<[string, AngebotenesFeld]> = []
  for (const [feld, gesichert] of Object.entries(bereich)) {
    const server = serverWert(feld)
    if (server === undefined || gleicherStand(server, gesichert.wert)) {
      erledigt.push([feld, gesichert.wert])
      continue
    }
    const konflikt = gesichert.vorher === undefined || !gleicherStand(server, gesichert.vorher)
    angebot.push([feld, { wert: gesichert.wert, server, konflikt, zeit: gesichert.zeit }])
  }
  return { erledigt: Object.fromEntries(erledigt), angebot: Object.fromEntries(angebot) }
}

/**
 * Vor dem Übernehmen: Felder, deren Wert auf der Seite sich seit dem Angebot geändert hat (hier bearbeitet, per
 * Echtzeit von anderen), werden nicht übernommen, sondern als Konflikt erneut angeboten.
 */
export function uebernehmbar(
  angebot: Record<string, AngebotenesFeld>,
  aktuell: (feld: string) => unknown,
): { setzen: Felder; zurueck: Record<string, AngebotenesFeld> } {
  const setzen: Array<[string, Wert]> = []
  const zurueck: Array<[string, AngebotenesFeld]> = []
  for (const [feld, angeboten] of Object.entries(angebot)) {
    const jetzt = aktuell(feld)
    if (gleicherStand(jetzt, angeboten.server)) setzen.push([feld, angeboten.wert])
    else if (jetzt !== undefined && !gleicherStand(jetzt, angeboten.wert)) {
      zurueck.push([feld, { ...angeboten, server: jetzt, konflikt: true }])
    }
  }
  return { setzen: Object.fromEntries(setzen), zurueck: Object.fromEntries(zurueck) }
}

/**
 * Bei jedem Seitenaufruf: abgelaufene und unlesbare Sicherungen löschen. Mit `konto` (angemeldete Seite) auch die
 * Sicherungen aller anderen Konten (die Anmeldung wechselte, ohne über „Abgemeldet“ zu gehen).
 */
export function aufraeumen(speicher: Storage | null = standardSpeicher(), jetzt = Date.now(), konto = ''): void {
  if (!speicher) return
  const eigene = GUELTIGE_KENNUNG.test(konto) ? `${PRAEFIX}${konto}.` : ''
  if (eigene) entferne(speicher, ABGEMELDET)
  for (const schluessel of schluesselMit(speicher, PRAEFIX)) {
    if (eigene && !schluessel.startsWith(eigene)) entferne(speicher, schluessel)
    else pruefe(speicher, schluessel, jetzt)
  }
}

/**
 * Alle Sicherungen aller Konten löschen (nach dem Abmelden); andere Einstellungen im Browser bleiben. Die Markierung
 * `ABGEMELDET` sperrt die Sicherung in noch offenen Seiten (`beiAbmeldungSperren`).
 */
export function alleLoeschen(speicher: Storage | null = standardSpeicher(), jetzt = Date.now()): void {
  if (!speicher) return
  for (const schluessel of schluesselMit(speicher, PRAEFIX)) entferne(speicher, schluessel)
  try {
    speicher.setItem(ABGEMELDET, String(jetzt))
  } catch {
    /* ohne Zugriff: nichts */
  }
}

/** Abmelden in einem anderen Tab: Diese Seite sichert danach nichts mehr (bis zum nächsten Aufruf) */
export function beiAbmeldungSperren(
  sicherung: Pick<Eingabensicherung, 'sperren'>,
  fenster: EventTarget | null = typeof window !== 'undefined' ? window : null,
): void {
  fenster?.addEventListener('storage', (event) => {
    const { key, newValue } = event as StorageEvent
    if (key === ABGEMELDET && newValue) sicherung.sperren()
  })
}
