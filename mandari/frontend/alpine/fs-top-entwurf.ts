/**
 * Formular „TOP vorschlagen“ bzw. „TOP eintragen“ der Mitglieder vor der Sitzung (Alpine-Komponente `fsTopEntwurf`,
 * Issue #1160): Was noch nicht beim Server angekommen ist, geht nicht verloren.
 *
 * Titel und Begründung liegen bis zur Annahme in der Sicherung im Browser (frontend/js/eingaben-sicherung.ts: je Konto,
 * Organisation und Sitzung, höchstens 24 Stunden, beim Abmelden gelöscht). Nach dem Neuladen, einer abgelaufenen
 * Anmeldung oder einer Störung beim Absenden stehen sie wieder im Formular. Hat der Server den TOP angenommen, steht
 * sein Titel auf der Seite (`data-top-titel`: eigene Vorschläge, Tagesordnung), und die Sicherung ist erledigt.
 *
 * Markup: templates/work/faction/neu/_vb_lesen.html.
 */

import { defineComponent } from '../js/alpine/component'
import { beiAbmeldungSperren, Eingabensicherung } from '../js/eingaben-sicherung'

/** Fester Name statt einer TOP-Kennung: Der TOP entsteht erst beim Absenden */
const TOP = 'neuer-top'
const BEREICH = 'entwurf'
const FELDER = ['title', 'description'] as const

type Eingabefeld = HTMLInputElement | HTMLTextAreaElement

function eingabefelder(formular: HTMLFormElement): Array<[string, Eingabefeld]> {
  const felder: Array<[string, Eingabefeld]> = []
  for (const name of FELDER) {
    const feld = formular.elements.namedItem(name)
    if (feld instanceof HTMLInputElement || feld instanceof HTMLTextAreaElement) felder.push([name, feld])
  }
  return felder
}

function titelSteht(titel: string): boolean {
  return [...document.querySelectorAll('[data-top-titel]')].some((el) => el.getAttribute('data-top-titel') === titel)
}

export const fsTopEntwurf = defineComponent(() => {
  let sicherung: Eingabensicherung | null = null
  let formular: HTMLFormElement | null = null

  return {
    wiederhergestellt: false,

    init() {
      formular = this.$el as HTMLFormElement
      const { konto = '', organisation = '', sitzung = '' } = formular.dataset
      const eigene = new Eingabensicherung(konto, organisation, sitzung)
      sicherung = eigene
      beiAbmeldungSperren(eigene)
      this.wiederherstellen()
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') eigene.festschreiben()
      })
      window.addEventListener('pagehide', () => eigene.festschreiben())
    },

    wiederherstellen() {
      if (!sicherung || !formular) return
      const bereich = sicherung.lesen(TOP)?.bereiche[BEREICH]
      if (!bereich) return
      const titel = String(bereich.title?.wert ?? '').trim()
      if (titel && titelSteht(titel)) {
        sicherung.entfernen(TOP)
        return
      }
      for (const [name, feld] of eingabefelder(formular)) {
        if (bereich[name]) feld.value = String(bereich[name].wert)
      }
      this.wiederhergestellt = true
    },

    merken() {
      if (!sicherung || !formular) return
      const felder = eingabefelder(formular)
      if (felder.every(([, feld]) => !feld.value.trim())) {
        sicherung.entfernen(TOP)
        this.wiederhergestellt = false
        return
      }
      sicherung.merken({ top: TOP, bereich: BEREICH }, Object.fromEntries(felder.map(([n, f]) => [n, f.value])))
    },

    /** Vor dem Absenden festschreiben: Kommt die Antwort nicht an, steht die Eingabe beim nächsten Aufruf wieder da */
    absenden() {
      this.merken()
      sicherung?.festschreiben()
    },

    verwerfen() {
      if (!formular) return
      for (const [, feld] of eingabefelder(formular)) feld.value = ''
      sicherung?.entfernen(TOP)
      this.wiederhergestellt = false
    },
  }
})
