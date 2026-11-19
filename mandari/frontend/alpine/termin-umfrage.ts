/**
 * Vorschläge einer neuen Terminumfrage in Work (templates/work/termine/umfrage_neu.html, Issue #1489).
 *
 * Markup: `<fieldset x-data="terminVorschlaege">` mit den Zeilen in `x-ref="liste"` (je Zeile Tag, Beginn, Ende und
 * „Entfernen“) und einer leeren Zeile als `<template x-ref="vorlage">`. Daneben ein Monatsblatt: Ein Klick auf einen
 * Tag füllt die erste leere Zeile oder hängt eine an und übernimmt die Uhrzeit des letzten Vorschlags; ein zweiter Klick
 * nimmt den Tag wieder heraus. Die Zeilen bleiben die einzige Quelle: Das Monatsblatt liest ihre Werte. Ohne
 * JavaScript bleiben die vorhandenen Zeilen nutzbar (Monatsblatt und Entfernen sind dann ausgeblendet); leere Zeilen
 * zählt der Server nicht.
 */

import { defineComponent } from '../js/alpine/component'

/** So viele Zeilen nimmt der Server höchstens an (apps/work/termine/umfrage.py, MAX_VORSCHLAEGE) */
const HOECHSTENS = 20
/** Uhrzeit, wenn noch kein Vorschlag eine hat */
const STANDARD_BEGINN = '18:00'

interface Tag {
  schluessel: string
  datum: string
  nr: number | ''
  gewaehlt: boolean
  vergangen: boolean
  heute: boolean
  beschriftung: string
}

/** Kalendertag als `JJJJ-MM-TT` in Ortszeit (wie `<input type="date">`) */
export function isoTag(tag: Date): string {
  const monat = String(tag.getMonth() + 1).padStart(2, '0')
  return `${tag.getFullYear()}-${monat}-${String(tag.getDate()).padStart(2, '0')}`
}

/** Tage eines Monatsblatts, Woche ab Montag; davor leere Felder bis zum ersten Wochentag */
export function monatsblatt(monat: Date, gewaehlt: Set<string>, heute: Date): Tag[] {
  const erster = new Date(monat.getFullYear(), monat.getMonth(), 1)
  const tage: Tag[] = []
  const versatz = (erster.getDay() + 6) % 7
  for (let i = 0; i < versatz; i++) {
    tage.push({
      schluessel: `leer-${i}`,
      datum: '',
      nr: '',
      gewaehlt: false,
      vergangen: true,
      heute: false,
      beschriftung: '',
    })
  }
  const heuteIso = isoTag(heute)
  const anzahl = new Date(monat.getFullYear(), monat.getMonth() + 1, 0).getDate()
  for (let nr = 1; nr <= anzahl; nr++) {
    const tag = new Date(monat.getFullYear(), monat.getMonth(), nr)
    const datum = isoTag(tag)
    tage.push({
      schluessel: datum,
      datum,
      nr,
      gewaehlt: gewaehlt.has(datum),
      vergangen: datum < heuteIso,
      heute: datum === heuteIso,
      beschriftung: tag.toLocaleDateString('de-DE', { weekday: 'long', day: 'numeric', month: 'long' }),
    })
  }
  return tage
}

export const terminVorschlaege = defineComponent(() => ({
  monat: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  /** zählt jede Änderung an den Zeilen hoch, damit das Monatsblatt sie neu liest */
  stand: 0,

  init() {
    const erster = this.zeilen()
      .map((zeile) => this.feld(zeile, 'tag')?.value)
      .find(Boolean)
    if (erster) {
      const [jahr, monat] = erster.split('-').map(Number)
      this.monat = new Date(jahr, monat - 1, 1)
    }
    this.$el.addEventListener('input', () => this.stand++)
    this.$el.addEventListener('change', (ereignis: Event) => {
      // Tag von Hand eingetragen: Uhrzeit wie beim Klick im Monatsblatt vorgeben
      const feld = ereignis.target
      if (feld instanceof HTMLInputElement && feld.name === 'vorschlag_tag' && feld.value) {
        const zeile = feld.closest('tr')
        if (zeile) this.zeitVorgeben(zeile)
      }
      this.stand++
    })
  },

  zeilen(): HTMLTableRowElement[] {
    const liste = this.$refs.liste as HTMLElement | undefined
    return liste ? Array.from(liste.querySelectorAll<HTMLTableRowElement>(':scope > tr')) : []
  },

  feld(zeile: Element, name: string): HTMLInputElement | null {
    return zeile.querySelector<HTMLInputElement>(`input[name="vorschlag_${name}"]`)
  },

  get monatName(): string {
    return this.monat.toLocaleDateString('de-DE', { month: 'long', year: 'numeric' })
  },

  get tage(): Tag[] {
    void this.stand
    const gewaehlt = new Set(this.zeilen().map((zeile) => this.feld(zeile, 'tag')?.value ?? ''))
    return monatsblatt(this.monat, gewaehlt, new Date())
  },

  blaettern(schritt: number) {
    this.monat = new Date(this.monat.getFullYear(), this.monat.getMonth() + schritt, 1)
  },

  /** Uhrzeit des letzten Vorschlags mit Beginn, als Vorgabe für einen neuen */
  letzteZeit(): { beginn: string; ende: string } {
    const mitZeit = this.zeilen()
      .filter((zeile) => this.feld(zeile, 'beginn')?.value)
      .pop()
    if (!mitZeit) return { beginn: STANDARD_BEGINN, ende: '' }
    return { beginn: this.feld(mitZeit, 'beginn')?.value ?? '', ende: this.feld(mitZeit, 'ende')?.value ?? '' }
  },

  /** Neue Zeile aus der Vorlage; `null`, wenn die Höchstzahl erreicht ist */
  anhaengen(): HTMLTableRowElement | null {
    const liste = this.$refs.liste as HTMLElement | undefined
    const vorlage = this.$refs.vorlage as HTMLTemplateElement | undefined
    if (!liste || !vorlage || this.zeilen().length >= HOECHSTENS) return null
    liste.append(vorlage.content.cloneNode(true))
    return liste.lastElementChild as HTMLTableRowElement
  },

  /** Leere Zeile anhängen; die Uhrzeit kommt erst mit dem Tag (`zeitVorgeben`), sonst zählte sie als Vorschlag */
  weitere() {
    const zeile = this.anhaengen()
    if (!zeile) return
    this.stand++
    this.feld(zeile, 'tag')?.focus()
  },

  /** Beginn und Ende des letzten Vorschlags übernehmen, nur gemeinsam und nur in eine Zeile ohne Beginn */
  zeitVorgeben(zeile: Element) {
    const beginn = this.feld(zeile, 'beginn')
    if (!beginn || beginn.value) return
    const zeit = this.letzteZeit()
    beginn.value = zeit.beginn
    const ende = this.feld(zeile, 'ende')
    if (ende && !ende.value) ende.value = zeit.ende
  },

  waehlen(datum: string) {
    if (!datum) return
    const mitTag = this.zeilen().filter((zeile) => this.feld(zeile, 'tag')?.value === datum)
    if (mitTag.length) {
      for (const zeile of mitTag) this.leeren(zeile)
      this.stand++
      return
    }
    const zeile = this.zeilen().find((z) => !this.feld(z, 'tag')?.value) ?? this.anhaengen()
    if (!zeile) return
    this.zeitVorgeben(zeile)
    const tag = this.feld(zeile, 'tag')
    if (tag) tag.value = datum
    this.stand++
  },

  /** Zeile entfernen; die letzten zwei bleiben stehen und werden nur geleert */
  leeren(zeile: HTMLTableRowElement) {
    if (this.zeilen().length > 2) {
      zeile.remove()
      return
    }
    for (const name of ['tag', 'beginn', 'ende']) {
      const feld = this.feld(zeile, name)
      if (feld) feld.value = ''
    }
  },

  entfernen(event: Event) {
    const zeile = (event.target as HTMLElement).closest('tr')
    if (zeile) this.leeren(zeile)
    this.stand++
  },
}))
