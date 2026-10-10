/**
 * Sicherung ungespeicherter Eingaben im Browser (#854, Teil 2).
 *
 * Was die Sitzungsvorbereitung an Feldern speichert (Position, Begründung, Notiz, Redebeitrag, Sitzungsnotizen),
 * liegt zusätzlich im localStorage, bis der Server genau diesen Stand bestätigt hat. So übersteht eine Eingabe ein
 * Neuladen, das Schließen der Seite oder einen Absturz des Browsers während eines offenen Speichervorgangs; beim
 * nächsten Aufruf bietet die Vorbereitung sie zur Übernahme an.
 *
 * - Schlüssel je Konto, Sitzung und TOP: `mandari.eingaben.<Konto-ID>.<Sitzung>.<TOP>`. Im Schlüssel steht die
 *   Kennung des Kontos, nie der Name; ohne Kennung wird nichts gesichert.
 * - Ein Feld verschwindet, sobald der Server genau diesen Wert bestätigt hat (eine neuere Eingabe bleibt bis zu
 *   ihrer eigenen Bestätigung), ein leerer Eintrag ganz. Klartext liegt also nur so lange im Browser wie nötig.
 * - Höchstens 24 Stunden nach der letzten Eingabe: Ältere Einträge werden beim Lesen und bei jedem Seitenaufruf
 *   gelöscht (`aufraeumen`, frontend/js/main.ts).
 * - Beim Abmelden werden alle Sicherungen gelöscht (`alleLoeschen`, Seite „Abgemeldet“).
 * - Ohne localStorage (privater Modus, Speicher voll) läuft alles ohne Sicherung weiter.
 *
 * Das Modul kennt weder Alpine noch den Speicherdienst; der Speicherdienst ruft `merken` und `gespeichert` über die
 * Schnittstelle `Sicherung` auf (frontend/js/speichern.ts).
 */

export const PRAEFIX = 'mandari.eingaben.'
/** Höchstens so lange nach der letzten Eingabe bleibt eine Sicherung erhalten */
export const HOECHSTDAUER_MS = 24 * 60 * 60 * 1000

export type Wert = string | number | boolean
export type Felder = Record<string, Wert>

/** Wohin eine Aktualisierung in der Sicherung gehört: TOP (bzw. ein fester Name) und Bereich (z. B. „notiz“) */
export interface SicherungsZiel {
  top: string
  bereich: string
}

export interface GesicherterEintrag {
  /** Zeitpunkt der letzten Eingabe (Millisekunden seit 1970) */
  zeit: number
  /** Felder je Bereich, z. B. `{ position: { reasoning: '…' }, notiz: { content: '…' } }` */
  bereiche: Record<string, Felder>
}

/** Was der Speicherdienst von der Sicherung braucht */
export interface Sicherung {
  /** Eingabe vor dem Senden festhalten */
  merken(ziel: SicherungsZiel, felder: Record<string, unknown>): void
  /** Der Server hat diese Felder mit genau diesen Werten gespeichert */
  gespeichert(ziel: SicherungsZiel, felder: Record<string, unknown>): void
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

function lies(speicher: Storage, schluessel: string): GesicherterEintrag | null {
  try {
    const roh = speicher.getItem(schluessel)
    if (!roh) return null
    const daten = JSON.parse(roh) as Partial<GesicherterEintrag> | null
    if (!daten || typeof daten.zeit !== 'number' || typeof daten.bereiche !== 'object' || daten.bereiche === null) {
      return null
    }
    return daten as GesicherterEintrag
  } catch {
    return null
  }
}

function entferne(speicher: Storage, schluessel: string): void {
  try {
    speicher.removeItem(schluessel)
  } catch {
    /* ohne Zugriff: nichts */
  }
}

function abgelaufen(eintrag: GesicherterEintrag | null, jetzt: number): boolean {
  return !eintrag || jetzt - eintrag.zeit > HOECHSTDAUER_MS
}

export class Eingabensicherung implements Sicherung {
  /** `mandari.eingaben.<Konto>.<Sitzung>.`; leer, wenn nicht gesichert werden darf */
  private readonly basis: string
  private readonly speicher: Storage | null
  private readonly jetzt: () => number

  constructor(konto: string, sitzung: string, optionen: { speicher?: Storage | null; jetzt?: () => number } = {}) {
    // Punkte trennen die Teile des Schlüssels; Kennungen mit Punkt würden fremde Einträge treffen
    const gueltig = /^[\w-]+$/
    this.basis = gueltig.test(konto) && gueltig.test(sitzung) ? `${PRAEFIX}${konto}.${sitzung}.` : ''
    this.speicher = optionen.speicher === undefined ? standardSpeicher() : optionen.speicher
    this.jetzt = optionen.jetzt || Date.now
  }

  get aktiv(): boolean {
    return !!this.basis && !!this.speicher
  }

  merken(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    const werte = Object.entries(felder).filter((paar): paar is [string, Wert] => istWert(paar[1]))
    if (!this.speicher || !this.basis || werte.length === 0) return
    const schluessel = this.basis + ziel.top
    const eintrag = this.gueltig(schluessel) || { zeit: 0, bereiche: {} }
    eintrag.bereiche[ziel.bereich] = { ...eintrag.bereiche[ziel.bereich], ...Object.fromEntries(werte) }
    eintrag.zeit = this.jetzt()
    this.schreibe(schluessel, eintrag)
  }

  gespeichert(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    this.vergessen(ziel, felder)
  }

  /** Diese Felder vergessen, sofern sie noch genau diese Werte haben (gespeichert oder verworfen) */
  vergessen(ziel: SicherungsZiel, felder: Record<string, unknown>): void {
    if (!this.speicher || !this.basis) return
    const schluessel = this.basis + ziel.top
    const eintrag = this.gueltig(schluessel)
    const bereich = eintrag?.bereiche[ziel.bereich]
    if (!eintrag || !bereich) return
    for (const [feld, wert] of Object.entries(felder)) {
      // Nur, was genau so gespeichert wurde; eine neuere Eingabe bleibt bis zu ihrer Bestätigung. Nur einfache
      // Werte vergleichen (geerbte Eigenschaften wie toString sind nie einfache Werte)
      if (istWert(wert) && bereich[feld] === wert) delete bereich[feld]
    }
    if (Object.keys(bereich).length === 0) delete eintrag.bereiche[ziel.bereich]
    if (Object.keys(eintrag.bereiche).length === 0) entferne(this.speicher, schluessel)
    else this.schreibe(schluessel, eintrag)
  }

  /** Ganzen Bereich vergessen, z. B. nach dem Löschen des Redebeitrags */
  verwerfen(ziel: SicherungsZiel): void {
    const bereich = this.lesen(ziel.top)?.bereiche[ziel.bereich]
    if (bereich) this.vergessen(ziel, bereich)
  }

  /** Alles zu einem TOP vergessen */
  entfernen(top: string): void {
    if (this.speicher && this.basis) entferne(this.speicher, this.basis + top)
  }

  /** Gesicherte Eingaben zu einem TOP (abgelaufene werden dabei gelöscht) */
  lesen(top: string): GesicherterEintrag | null {
    if (!this.speicher || !this.basis) return null
    return this.gueltig(this.basis + top)
  }

  /** Alle gesicherten Eingaben dieses Kontos zu dieser Sitzung (abgelaufene werden dabei gelöscht) */
  alle(): Array<{ top: string; eintrag: GesicherterEintrag }> {
    if (!this.speicher || !this.basis) return []
    const liste: Array<{ top: string; eintrag: GesicherterEintrag }> = []
    for (const schluessel of schluesselMit(this.speicher, this.basis)) {
      const eintrag = this.gueltig(schluessel)
      if (eintrag) liste.push({ top: schluessel.slice(this.basis.length), eintrag })
    }
    return liste
  }

  private gueltig(schluessel: string): GesicherterEintrag | null {
    if (!this.speicher) return null
    const eintrag = lies(this.speicher, schluessel)
    if (!abgelaufen(eintrag, this.jetzt())) return eintrag
    // Abgelaufen oder unlesbar: weg damit
    entferne(this.speicher, schluessel)
    return null
  }

  private schreibe(schluessel: string, eintrag: GesicherterEintrag): void {
    if (!this.speicher) return
    try {
      this.speicher.setItem(schluessel, JSON.stringify(eintrag))
    } catch {
      // Speicher voll: Abgelaufenes räumen und einmal erneut versuchen, sonst ohne Sicherung weiter
      aufraeumen(this.speicher, this.jetzt())
      try {
        this.speicher.setItem(schluessel, JSON.stringify(eintrag))
      } catch {
        /* ohne Sicherung weiter */
      }
    }
  }
}

/** Abgelaufene und unlesbare Sicherungen aller Konten löschen (bei jedem Seitenaufruf) */
export function aufraeumen(speicher: Storage | null = standardSpeicher(), jetzt = Date.now()): void {
  if (!speicher) return
  for (const schluessel of schluesselMit(speicher, PRAEFIX)) {
    if (abgelaufen(lies(speicher, schluessel), jetzt)) entferne(speicher, schluessel)
  }
}

/** Alle Sicherungen aller Konten löschen (nach dem Abmelden); andere Einstellungen im Browser bleiben */
export function alleLoeschen(speicher: Storage | null = standardSpeicher()): void {
  if (!speicher) return
  for (const schluessel of schluesselMit(speicher, PRAEFIX)) entferne(speicher, schluessel)
}
