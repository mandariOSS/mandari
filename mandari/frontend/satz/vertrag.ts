/**
 * Satz-Kette für Dokumente (Issue #1584): gemeinsame Datentypen.
 *
 * Die Kette setzt den Text des Editors auf echte Seiten, und zwar im Browser genauso wie beim Erzeugen des PDFs
 * (dieselbe Kette läuft dort in einem headless Chromium):
 *
 *   messen.ts    liest aus dem DOM, wo Blöcke und Zeilen ohne Seitenumbrüche stehen („natürliche Lage“)
 *   umbruch.ts   verteilt sie auf Seiten und berechnet die Lücken, die Inhalt auf die nächste Seite schieben
 *   erweiterung.ts  setzt die Lücken als ProseMirror-Dekorationen in den sichtbaren, bearbeitbaren Text
 *   boegen.ts    zeichnet hinter dem Text die Seiten mit Briefpapier, Kopf, Fuß und Seitenzahlen
 *
 * Alle Längen sind CSS-Pixel des Layouts (96 je Zoll), unabhängig vom Zoom der Ansicht.
 */

/** Seitenränder einer Seite (oben/unten) in px */
export interface Raender {
  oben: number
  unten: number
}

export interface Seitengeometrie {
  breite: number
  hoehe: number
  links: number
  rechts: number
  /** Erste Seite (beim generierten Briefkopf mit Kopf) */
  erste: Raender
  /** Ab Seite 2 */
  folge: Raender
  /** Abstand zwischen zwei Seiten (Editor: sichtbare Lücke, Druck: 0) */
  luecke: number
}

export type BlockArt = 'text' | 'kopf' | 'zeile' | 'atom' | 'umbruch'

/** Eine Zeile eines Textblocks: Zeilenkasten in natürlicher Lage und Position ihres ersten Zeichens */
export interface Zeile {
  oben: number
  unten: number
  /** ProseMirror-Position des ersten Zeichens der Zeile; null, wenn sie sich nicht bestimmen ließ */
  pos: number | null
}

export interface MessBlock {
  art: BlockArt
  /** Rahmenkante oben und unten in natürlicher Lage (relativ zur Oberkante des Textelements) */
  oben: number
  unten: number
  /** Zeilen eines Textblocks (leer bei Tabellenzeilen, Bildern, Umbrüchen) */
  zeilen: Zeile[]
  /** Position, an der eine Lücke den ganzen Block (samt Listenpunkt, Zitatlinie) auf die nächste Seite schiebt */
  anker: number
  /** Mit dem nächsten Block auf derselben Seite halten (Überschriften) */
  mitNaechstem: boolean
  /** Nicht zwischen Zeilen teilen */
  zusammen: boolean
  /** Tabellenzeile: Zahl der Spalten (die Lücke davor überspannt genau so viele) */
  spalten?: number
}

export type LueckenArt = 'block' | 'zeile' | 'tabelle'

export interface Luecke {
  art: LueckenArt
  /** ProseMirror-Position der Lücke */
  pos: number
  /** Höhe in px */
  px: number
  /** Tabellenlücke: Zahl der Spalten der Tabelle */
  spalten?: number
}

export interface Umbruch {
  seiten: number
  luecken: Luecke[]
  /** Blöcke, die höher als eine Seite sind und über den Seitenrand ragen (Hinweis an die Person) */
  zuHoch: number
}

/** Server-Konfiguration des Satzes (apps/work/motions/satz.py) */
export interface SatzKonfig {
  seite: { breite_mm: number; hoehe_mm: number }
  raender: { oben: number; unten: number; links: number; rechts: number; oben_folge: number; unten_folge: number }
  schrift: { schluessel: string; stapel: string; groesse_pt: number; zeilenabstand: number }
  briefpapier: {
    art: '' | 'pdf' | 'generiert'
    pdf_url: string
    /** Kopf der ersten Seite (generierter Briefkopf), vom Server gerendert */
    kopf_html: string
    fuss_zeilen: string[]
    seitenzahlen: boolean
  }
  /** Schriftdateien (Schlüssel → CSS-Familie), die der Text braucht */
  schriften: { schluessel: string; name: string }[]
}

export const PX_JE_MM = 96 / 25.4

export function mmZuPx(mm: number): number {
  return mm * PX_JE_MM
}

export function geometrieAus(konfig: SatzKonfig, luecke: number, ersteObenMin = 0): Seitengeometrie {
  const r = konfig.raender
  return {
    breite: mmZuPx(konfig.seite.breite_mm),
    hoehe: mmZuPx(konfig.seite.hoehe_mm),
    links: mmZuPx(r.links),
    rechts: mmZuPx(r.rechts),
    erste: { oben: Math.max(mmZuPx(r.oben), ersteObenMin), unten: mmZuPx(r.unten) },
    folge: { oben: mmZuPx(r.oben_folge), unten: mmZuPx(r.unten_folge) },
    luecke,
  }
}

/** Oberkante der Seite `nr` (0-basiert) */
export function seitenAnfang(g: Seitengeometrie, nr: number): number {
  return nr * (g.hoehe + g.luecke)
}

/** Oberkante des Satzspiegels der Seite `nr` */
export function satzOben(g: Seitengeometrie, nr: number): number {
  return seitenAnfang(g, nr) + (nr === 0 ? g.erste.oben : g.folge.oben)
}

/** Unterkante des Satzspiegels der Seite `nr` */
export function satzUnten(g: Seitengeometrie, nr: number): number {
  return seitenAnfang(g, nr) + g.hoehe - (nr === 0 ? g.erste.unten : g.folge.unten)
}

/** Gesamthöhe aller Seiten samt Lücken dazwischen */
export function stapelHoehe(g: Seitengeometrie, seiten: number): number {
  return seiten * g.hoehe + Math.max(0, seiten - 1) * g.luecke
}
