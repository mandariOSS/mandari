/**
 * Schriften der Satz-Kette (Issue #1584): erst messen, wenn alle Schnitte geladen sind.
 *
 * `document.fonts.ready` wartet nur auf Schriften, die schon gebraucht werden; Fett oder Kursiv fehlen dann womöglich
 * noch, und die Zeilen würden mit der Ersatzschrift gemessen. Deshalb lädt die Kette jeden Schnitt der verwendeten
 * Familien ausdrücklich. Die Dateien und @font-face-Regeln liefert static/vendor/schriften/schriften.css.
 */

const SCHNITTE = ['normal 400', 'normal 700', 'italic 400', 'italic 700'] as const

const geladen = new Map<string, Promise<void>>()

/** Alle vier Schnitte einer Familie laden (einmal je Seite) */
export function familieLaden(name: string): Promise<void> {
  let laden = geladen.get(name)
  if (!laden) {
    const familie = `"${name.replace(/"/g, '')}"`
    laden = Promise.allSettled(
      SCHNITTE.map((schnitt) =>
        document.fonts ? document.fonts.load(`${schnitt} 16px ${familie}`) : Promise.resolve([]),
      ),
    ).then(() => undefined)
    geladen.set(name, laden)
  }
  return laden
}

/** Familiennamen aus `font-family`-Angaben im Text (erste Angabe, ohne Anführungszeichen) */
export function familienImText(wurzel: HTMLElement): string[] {
  const namen = new Set<string>()
  for (const el of Array.from(wurzel.querySelectorAll<HTMLElement>('[style*="font-family"]'))) {
    const erste = el.style.fontFamily
      .split(',')[0]
      ?.trim()
      .replace(/^["']|["']$/g, '')
    if (erste) namen.add(erste)
  }
  return [...namen]
}

export async function schriftenLaden(namen: string[]): Promise<void> {
  await Promise.all(namen.map((name) => familieLaden(name)))
  if (document.fonts) await document.fonts.ready
}
