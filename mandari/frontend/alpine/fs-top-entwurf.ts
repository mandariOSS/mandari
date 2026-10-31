/**
 * Formular „TOP vorschlagen“ bzw. „TOP eintragen“ der Mitglieder vor der Sitzung (Alpine-Komponente `fsTopEntwurf`,
 * Issue #1160): Was noch nicht beim Server angekommen ist, geht nicht verloren.
 *
 * Titel, Begründung und die Auswahl des Teils liegen bis zur Annahme in der Sicherung im Browser
 * (frontend/js/eingaben-sicherung.ts: je Konto, Organisation und Sitzung, höchstens 24 Stunden, beim Abmelden gelöscht).
 * Nach dem Neuladen, einer abgelaufenen Anmeldung oder einer Störung beim Absenden stehen sie wieder im Formular. Die
 * Auswahl gehört dazu: Sonst stünde ein vertraulicher TOP danach wieder auf „öffentlich“. Erledigt ist die Sicherung,
 * wenn die Eingabe abgeschickt wurde und ihr Titel auf der Seite steht (`data-top-titel`: eigene Vorschläge,
 * Tagesordnung). Ein nur getippter Titel, den es schon gibt, bleibt erhalten.
 *
 * Markup: templates/work/faction/neu/_vb_lesen.html.
 */

import { defineComponent } from '../js/alpine/component'
import { beiAbmeldungSperren, Eingabensicherung } from '../js/eingaben-sicherung'

/** Fester Name statt einer TOP-Kennung: Der TOP entsteht erst beim Absenden */
const TOP = 'neuer-top'
const BEREICH = 'entwurf'
const ZIEL = { top: TOP, bereich: BEREICH }
const TEXTFELDER = ['title', 'description'] as const
const AUSWAHLFELDER = ['visibility', 'parent_id'] as const

type Textfeld = HTMLInputElement | HTMLTextAreaElement

function textfelder(formular: HTMLFormElement): Array<[string, Textfeld]> {
  const felder: Array<[string, Textfeld]> = []
  for (const name of TEXTFELDER) {
    const feld = formular.elements.namedItem(name)
    if (feld instanceof HTMLInputElement || feld instanceof HTMLTextAreaElement) felder.push([name, feld])
  }
  return felder
}

function auswahlfelder(formular: HTMLFormElement): Array<[string, HTMLSelectElement]> {
  const felder: Array<[string, HTMLSelectElement]> = []
  for (const name of AUSWAHLFELDER) {
    const feld = formular.elements.namedItem(name)
    if (feld instanceof HTMLSelectElement) felder.push([name, feld])
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
      if (bereich.abgesendet?.wert === true && titel && titelSteht(titel)) {
        sicherung.entfernen(TOP)
        return
      }
      for (const [name, feld] of textfelder(formular)) {
        if (bereich[name]) feld.value = String(bereich[name].wert)
      }
      for (const [name, feld] of auswahlfelder(formular)) {
        if (!bereich[name]) continue
        feld.value = String(bereich[name].wert)
        // Die gewählte Stelle gibt es nicht mehr (etwa ein gelöschter TOP): gültige Auswahl statt keiner
        if (feld.selectedIndex < 0) feld.selectedIndex = 0
      }
      this.wiederhergestellt = true
    },

    merken() {
      if (!sicherung || !formular) return
      const texte = textfelder(formular)
      if (texte.every(([, feld]) => !feld.value.trim())) {
        sicherung.entfernen(TOP)
        this.wiederhergestellt = false
        return
      }
      const felder = [...texte, ...auswahlfelder(formular)].map(([name, feld]) => [name, feld.value])
      sicherung.merken(ZIEL, { ...Object.fromEntries(felder), abgesendet: false })
    },

    /** Vor dem Absenden festschreiben: Kommt die Antwort nicht an, steht die Eingabe beim nächsten Aufruf wieder da */
    absenden() {
      this.merken()
      sicherung?.merken(ZIEL, { abgesendet: true })
      sicherung?.festschreiben()
    },

    verwerfen() {
      if (!formular) return
      for (const [, feld] of textfelder(formular)) feld.value = ''
      for (const [, feld] of auswahlfelder(formular)) feld.selectedIndex = 0
      sicherung?.entfernen(TOP)
      this.wiederhergestellt = false
    },
  }
})
