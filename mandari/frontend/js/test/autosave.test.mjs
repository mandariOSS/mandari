/**
 * Tests für die Bewertung automatisch speichernder htmx-Formulare (`frontend/js/autosave.ts`, #854 Teil 2):
 * Auch 4xx, eine abgelaufene Anmeldung und die Pflicht zum zweiten Faktor gelten als „Nicht gespeichert“. Dazu die
 * Anzeige (`frontend/alpine/autosave-anzeige.ts`): Fehler je Formular, damit ein gelungenes Formular den Fehler eines
 * anderen im selben Panel nicht verdeckt.
 *
 * Ausführen mit: npm run test:speichern
 * (baut beide Module via esbuild nach CJS)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const build = (quelle, name) => {
  const ziel = join(here, 'build', `${name}.cjs`)
  execSync(`npx esbuild ${quelle} --bundle --format=cjs --platform=node --outfile="${ziel}"`, {
    cwd: projectRoot,
    stdio: 'inherit',
  })
  return ziel
}

// Die Anzeige nutzt window.setTimeout und window.dispatchEvent
globalThis.window = globalThis

const require = createRequire(import.meta.url)
const { bewerteAutosave, EINGABE_PRUEFEN } = require(build('frontend/js/autosave.ts', 'autosave'))
const { autosaveZustand, autosaveAnzeige } = require(build('frontend/alpine/autosave-anzeige.ts', 'autosave-anzeige'))

const HERKUNFT = 'https://mandari.example'
const ZIEL = `${HERKUNFT}/work/fraktion/faction/1/item/2/action/`

function antwort(teile = {}) {
  return {
    status: 200,
    adresse: ZIEL,
    umleitung: null,
    inhaltstyp: 'text/html; charset=utf-8',
    text: '',
    herkunft: HERKUNFT,
    ...teile,
  }
}

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed++
    console.log(`  OK   ${name}`)
  } catch (err) {
    failed++
    console.error(`  FAIL ${name}`)
    console.error(`       ${err.stack || err.message}`)
  }
}

test('Gespeichert (200, 204): kein Fehler', () => {
  assert.equal(bewerteAutosave(antwort()), null)
  assert.equal(bewerteAutosave(antwort({ status: 204 })), null)
})

test('403 (keine Berechtigung) ist sichtbar und endgültig', () => {
  const f = bewerteAutosave(antwort({ status: 403 }))
  assert.equal(f.art, 'abgelehnt')
  assert.match(f.meldung, /^Nicht gespeichert: Keine Berechtigung/)
  assert.equal(f.ziel, '')
})

test('403 als HTML-Seite (z. B. CSRF) zeigt keinen Seiteninhalt', () => {
  const f = bewerteAutosave(antwort({ status: 403, text: '<!DOCTYPE html><html>…</html>' }))
  assert.equal(f.meldung, 'Nicht gespeichert: Keine Berechtigung für diese Änderung.')
})

test('400 ist sichtbar, mit kurzem Klartext des Servers', () => {
  assert.equal(
    bewerteAutosave(antwort({ status: 400 })).meldung,
    'Nicht gespeichert: Die Eingabe wurde nicht angenommen.',
  )
  const f = bewerteAutosave(antwort({ status: 400, inhaltstyp: 'text/plain', text: 'Inhalt ist erforderlich.' }))
  assert.equal(f.art, 'abgelehnt')
  assert.equal(f.meldung, 'Nicht gespeichert: Inhalt ist erforderlich.')
  const j = bewerteAutosave(
    antwort({ status: 400, inhaltstyp: 'application/json', text: JSON.stringify({ error: 'Titel fehlt' }) }),
  )
  assert.equal(j.meldung, 'Nicht gespeichert: Titel fehlt')
})

test('404: Eintrag gibt es nicht mehr', () => {
  assert.equal(bewerteAutosave(antwort({ status: 404 })).art, 'abgelehnt')
})

test('Abgelaufene Anmeldung: Umleitung auf die Anmeldeseite (200) gilt nicht als gespeichert', () => {
  const f = bewerteAutosave(antwort({ adresse: `${HERKUNFT}/accounts/login/?next=/work/x/` }))
  assert.equal(f.art, 'anmeldung')
  assert.match(f.meldung, /Anmeldung ist abgelaufen/)
  assert.match(f.meldung, /Ihre Eingabe wird danach automatisch gespeichert/)
  assert.equal(f.ziel, '/accounts/login/')
  assert.equal(f.zielText, 'In neuem Tab anmelden')
  assert.equal(bewerteAutosave(antwort({ status: 401 })).art, 'anmeldung')
})

test('Pflicht zum zweiten Faktor (204 mit HX-Redirect): Hinweis mit Link, keine Umleitung der Seite', () => {
  const f = bewerteAutosave(antwort({ status: 204, umleitung: '/accounts/2fa/setup/' }))
  assert.equal(f.art, 'anmeldung')
  assert.match(f.meldung, /zweiten Faktor einrichten/)
  assert.equal(f.ziel, '/accounts/2fa/setup/')
  assert.equal(f.zielText, 'Zweiten Faktor einrichten')
})

test('403 two_factor_setup_required als JSON: Hinweis mit Link', () => {
  const f = bewerteAutosave(
    antwort({
      status: 403,
      inhaltstyp: 'application/json',
      text: JSON.stringify({ error: 'two_factor_setup_required', redirect: '/accounts/security-key/' }),
    }),
  )
  assert.equal(f.art, 'anmeldung')
  assert.equal(f.ziel, '/accounts/security-key/')
})

test('Ziele anderer Herkunft werden nicht verlinkt', () => {
  const f = bewerteAutosave(antwort({ status: 204, umleitung: 'https://boese.example/phish' }))
  assert.equal(f.ziel, '/accounts/login/')
})

test('Keine Verbindung (Status 0) und Serverfehler: erneut versuchen hilft', () => {
  assert.equal(bewerteAutosave(antwort({ status: 0 })).art, 'verbindung')
  for (const status of [500, 502, 503, 504, 429, 408]) {
    const f = bewerteAutosave(antwort({ status }))
    assert.equal(f.art, 'server', `Status ${status}`)
    assert.match(f.meldung, /^Nicht gespeichert/)
  }
})

test('422 mit neu gezeichnetem Formular (markierte Felder): Hinweis auf die markierten Angaben', () => {
  const f = bewerteAutosave(antwort({ status: 422, text: '<div x-data="autosaveAnzeige">…</div>' }))
  assert.equal(f.art, 'abgelehnt')
  assert.equal(f.meldung, EINGABE_PRUEFEN)
})

const KOPF = 'agenda-item-update-form'
const BESCHREIBUNG = 'autosave-1'

test('Panel mit zwei Formularen: Kopf scheitert, Beschreibung gelingt, der Hinweis bleibt', () => {
  const z = autosaveZustand()
  z.markSaving()
  z.markFailed({ ...bewerteAutosave(antwort({ status: 403 })), formular: KOPF })
  z.markSaving()
  z.markSaved({ formular: BESCHREIBUNG })
  assert.equal(z.saving, false)
  assert.equal(z.saved, false, 'kein Haken, solange der Kopf nicht gespeichert ist')
  assert.match(z.saveFehler.meldung, /Keine Berechtigung/)
  // Meldung des Servers per HX-Trigger (ohne Kennung) ändert nichts
  z.markSaved({ value: true })
  z.markSaved()
  assert.match(z.saveFehler.meldung, /Keine Berechtigung/)
  // Gelingt der Kopf danach, verschwindet der Hinweis
  z.markSaved({ formular: KOPF })
  assert.equal(z.saveFehler, null)
  assert.equal(z.saved, true)
})

test('Zwei gescheiterte Formulare: Gelingt eines, bleibt der Fehler des anderen', () => {
  const z = autosaveZustand()
  z.markFailed({ ...bewerteAutosave(antwort({ status: 0 })), formular: KOPF })
  z.markFailed({ ...bewerteAutosave(antwort({ status: 400 })), formular: BESCHREIBUNG })
  z.markSaved({ formular: BESCHREIBUNG })
  assert.equal(z.saveFehler.art, 'verbindung')
  assert.equal(z.saved, false)
})

test('Vom Server neu gezeichnetes Panel nach Ablehnung (422): Hinweis von Anfang an, bis das Formular gelingt', () => {
  const k = autosaveAnzeige()
  k.$el = { dataset: { autosaveAbgelehnt: 'task-update-form' } }
  k.init()
  assert.equal(k.saveFehler.meldung, EINGABE_PRUEFEN)
  assert.equal(k.saved, false)
  k.markSaved({ formular: 'task-update-form' })
  assert.equal(k.saveFehler, null)
  const ohne = autosaveAnzeige()
  ohne.$el = { dataset: {} }
  ohne.init()
  assert.equal(ohne.saveFehler, null)
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
process.exit(failed > 0 ? 1 : 0)
