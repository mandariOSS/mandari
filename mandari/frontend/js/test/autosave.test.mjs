/**
 * Tests für die Bewertung automatisch speichernder htmx-Formulare (`frontend/js/autosave.ts`, #854 Teil 2):
 * Auch 4xx, eine abgelaufene Anmeldung und die Pflicht zum zweiten Faktor gelten als „Nicht gespeichert“.
 *
 * Ausführen mit: npm run test:speichern
 * (baut autosave.ts via esbuild nach CJS)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'autosave.cjs')

execSync(`npx esbuild frontend/js/autosave.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`, {
  cwd: projectRoot,
  stdio: 'inherit',
})

const require = createRequire(import.meta.url)
const { bewerteAutosave } = require(outFile)

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

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
process.exit(failed > 0 ? 1 : 0)
