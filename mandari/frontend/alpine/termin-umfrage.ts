/**
 * Vorschläge einer neuen Terminumfrage in Work (templates/work/termine/umfrage_neu.html, Issue #1489).
 *
 * Markup: `<fieldset x-data="terminVorschlaege">` mit den Zeilen in `x-ref="liste"` und einer leeren Zeile als
 * `<template x-ref="vorlage">`. „Weiterer Vorschlag“ hängt eine Kopie der Vorlage an und setzt den Fokus in ihr
 * erstes Feld. Ohne JavaScript bleiben die vorhandenen Zeilen nutzbar; leere Zeilen zählt der Server nicht.
 */

import { defineComponent } from '../js/alpine/component'

/** So viele Zeilen nimmt der Server höchstens an (apps/work/termine/umfrage.py, MAX_VORSCHLAEGE) */
const HOECHSTENS = 20

export const terminVorschlaege = defineComponent(() => ({
  weitere() {
    const liste = this.$refs.liste as HTMLElement | undefined
    const vorlage = this.$refs.vorlage as HTMLTemplateElement | undefined
    if (!liste || !vorlage || liste.children.length >= HOECHSTENS) return
    liste.append(vorlage.content.cloneNode(true))
    liste.lastElementChild?.querySelector<HTMLElement>('input')?.focus()
  },
}))
