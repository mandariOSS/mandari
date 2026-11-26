/**
 * Druck der Satz-Kette (Issue #1584): Vite-Einstieg der Druckansicht (templates/work/motions/satz/druck.html).
 *
 * Setzt den Text mit denselben Erweiterungen und derselben Kette wie die Seitenansicht des Editors, nur ohne Lücke
 * zwischen den Seiten und ohne Briefpapier-Bild (das legt der Server als Vektor unter das PDF). Danach meldet die Seite
 * `window.satzFertig = true`; darauf wartet die PDF-Erzeugung (headless Chromium, apps/common/pdf_chromium.py).
 * Scheitert der Satz, wirft die Seite einen Fehler in die Konsole: Die Erzeugung bricht dann ab und der Server nimmt
 * den bisherigen Weg.
 */

import { Editor } from '@tiptap/core'
import { dokumentErweiterungen, dokumentStarterKit } from '../editor/basis'
import { SatzSteuerung } from './steuerung'
import type { SatzKonfig } from './vertrag'

declare global {
  interface Window {
    satzFertig?: boolean
    satzSeiten?: number
  }
}

async function setzen(): Promise<void> {
  const konfigEl = document.getElementById('satz-konfig')
  const inhaltEl = document.getElementById('satz-inhalt') as HTMLTemplateElement | null
  const rahmen = document.getElementById('editor-container')
  if (!konfigEl?.textContent || !inhaltEl || !rahmen) throw new Error('Druckansicht unvollständig')
  const konfig = JSON.parse(konfigEl.textContent) as SatzKonfig

  const editor = new Editor({
    element: rahmen,
    editable: false,
    content: inhaltEl.innerHTML,
    extensions: [dokumentStarterKit(), ...dokumentErweiterungen({ satz: true, placeholder: '' })],
  })
  const satz = new SatzSteuerung(editor, rahmen, { luecke: 0, druck: true })
  const ergebnis = await satz.setzen(konfig)
  if (!ergebnis) throw new Error('Satz ohne Ergebnis')
  // Ein zweiter Durchlauf mit allen geladenen Schriften (auch denen, die erst der erste Satz angefordert hat)
  const zweiter = await satz.jetzt()
  window.satzSeiten = (zweiter ?? ergebnis).seiten
  window.satzFertig = true
}

setzen().catch((err: unknown) => {
  console.error('Satz fehlgeschlagen:', err)
  // Uncaught: Die PDF-Erzeugung bricht bei einer Ausnahme in der Konsole ab (failOnConsoleExceptions)
  window.setTimeout(() => {
    window.satzFertig = true
    throw err instanceof Error ? err : new Error(String(err))
  })
})
