/**
 * Automatisch speichernde htmx-Formulare (`data-autosave="<name>"`: TOP einer Fraktionssitzung, Aufgaben; #854 Teil 2).
 *
 * Bisher meldeten sie nur Serverfehler (5xx) per Toast. Ein 400 oder 403 blieb stumm und die Anzeige stand dauerhaft
 * auf „Speichert …“; eine abgelaufene Anmeldung galt sogar als gespeichert (fetch folgt der Umleitung auf die
 * Anmeldeseite und bekommt 200), und die Pflicht zum zweiten Faktor lud die Seite neu (`HX-Redirect`), womit die
 * Eingabe weg war. Hier wird die Antwort bewertet; frontend/js/htmx-setup.ts meldet das Ergebnis als Ereignis
 * `<name>-autosaved` bzw. `<name>-autosave-failed` (Detail: `AutosaveFehler`), die Anzeige steht in
 * frontend/alpine/autosave-anzeige.ts. Die Texte folgen dem Speicherdienst der Vorbereitung (frontend/js/speichern.ts).
 */

/**
 * - `anmeldung`: Anmeldung abgelaufen bzw. zweiter Faktor fehlt (nach neuer Anmeldung erneut senden)
 * - `verbindung`: keine Antwort (erneut senden, sobald wieder online)
 * - `server`: Fehler auf dem Server oder vorübergehend nicht erreichbar (erneut versuchen hilft oft)
 * - `abgelehnt`: endgültig (400, 403, 404 …), erneut senden ändert nichts
 */
export type AutosaveArt = 'anmeldung' | 'verbindung' | 'server' | 'abgelehnt'

export interface AutosaveFehler {
  art: AutosaveArt
  /** Ganze Erklärung für den Hinweis unter dem Formular */
  meldung: string
  /** Adresse für Anmeldung bzw. Einrichtung des zweiten Faktors (gleiche Herkunft, öffnet im neuen Tab) */
  ziel: string
  /** Text des Links zu `ziel` */
  zielText: string
}

export interface AutosaveAntwort {
  /** HTTP-Status; 0 ohne Antwort (Netzfehler, Abbruch, Zeitüberschreitung) */
  status: number
  /** Adresse der endgültigen Antwort nach Umleitungen (`xhr.responseURL`) */
  adresse: string
  /** Kopfzeile `HX-Redirect` (die Pflicht zum zweiten Faktor schickt htmx-Anfragen so zur Einrichtung) */
  umleitung: string | null
  inhaltstyp: string
  /** Antworttext (nur bei Fehlern gelesen, gekürzt) */
  text: string
  /** Herkunft der Seite (`location.origin`) für die Prüfung der Ziele */
  herkunft: string
}

export const ANMELDESEITE = '/accounts/login/'

const ANMELDUNG_ABGELAUFEN =
  'Nicht gespeichert: Ihre Anmeldung ist abgelaufen. Bitte in einem neuen Tab anmelden, ihn dann schließen und hier weiterarbeiten. Ihre Eingabe wird danach automatisch gespeichert.'
const ZWEITER_FAKTOR =
  'Nicht gespeichert: Ihr Konto muss zuerst einen zweiten Faktor einrichten. Bitte in einem neuen Tab erledigen, ihn dann schließen und hier weiterarbeiten. Ihre Eingabe wird danach automatisch gespeichert.'
const VORUEBERGEHEND = new Set([408, 425, 429, 502, 503, 504])

/** Nur Ziele derselben Herkunft (sonst die Anmeldeseite) */
function sicheresZiel(ziel: string, herkunft: string): string {
  try {
    const adresse = new URL(ziel, herkunft)
    if (adresse.origin === new URL(herkunft).origin) return adresse.pathname + adresse.search
  } catch {
    /* ungültig: Anmeldeseite */
  }
  return ANMELDESEITE
}

function pfad(adresse: string, herkunft: string): string {
  try {
    return new URL(adresse, herkunft).pathname
  } catch {
    return ''
  }
}

function json(antwort: AutosaveAntwort): Record<string, unknown> | null {
  if (!antwort.inhaltstyp.includes('application/json')) return null
  try {
    const daten = JSON.parse(antwort.text) as unknown
    return typeof daten === 'object' && daten !== null ? (daten as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** Kurzer Klartext des Servers (z. B. „Inhalt ist erforderlich.“), keine HTML-Seite */
function serverText(antwort: AutosaveAntwort): string {
  const daten = json(antwort)
  if (typeof daten?.error === 'string') return daten.error
  const text = antwort.text.trim()
  if (!text || text.length > 200 || text.startsWith('<') || antwort.inhaltstyp.includes('json')) return ''
  return text
}

function fehler(art: AutosaveArt, meldung: string, ziel = '', zielText = ''): AutosaveFehler {
  return { art, meldung, ziel, zielText }
}

/** `null`, wenn gespeichert; sonst Grund und Erklärung */
export function bewerteAutosave(antwort: AutosaveAntwort): AutosaveFehler | null {
  const { status, herkunft } = antwort
  if (antwort.umleitung) {
    return fehler('anmeldung', ZWEITER_FAKTOR, sicheresZiel(antwort.umleitung, herkunft), 'Zweiten Faktor einrichten')
  }
  if (status === 0) {
    return fehler(
      'verbindung',
      'Nicht gespeichert: keine Verbindung. Die Eingabe bleibt im Formular und wird gespeichert, sobald die Verbindung wieder steht.',
    )
  }
  // Abgelaufene Anmeldung: Django leitet auf die Anmeldeseite um, der Browser folgt und liefert sie mit 200
  const anmeldeseite = pfad(antwort.adresse, herkunft) === ANMELDESEITE
  if (status === 401 || anmeldeseite)
    return fehler('anmeldung', ANMELDUNG_ABGELAUFEN, ANMELDESEITE, 'In neuem Tab anmelden')
  if (status === 403 && json(antwort)?.error === 'two_factor_setup_required') {
    const ziel = String(json(antwort)?.redirect || '')
    return fehler('anmeldung', ZWEITER_FAKTOR, sicheresZiel(ziel, herkunft), 'Zweiten Faktor einrichten')
  }
  if (status >= 200 && status < 300) return null
  if (VORUEBERGEHEND.has(status) || status >= 500) {
    return fehler('server', 'Nicht gespeichert: Fehler auf dem Server. Bitte erneut versuchen.')
  }
  const text = serverText(antwort)
  if (status === 403) {
    return fehler('abgelehnt', `Nicht gespeichert: Keine Berechtigung für diese Änderung.${text ? ` (${text})` : ''}`)
  }
  if (status === 404) return fehler('abgelehnt', 'Nicht gespeichert: Den Eintrag gibt es nicht mehr.')
  if (status === 400 || status === 422) {
    return fehler('abgelehnt', `Nicht gespeichert: ${text || 'Die Eingabe wurde nicht angenommen.'}`)
  }
  return fehler('abgelehnt', `Nicht gespeichert (Fehler ${status}).${text ? ` ${text}` : ''}`)
}
