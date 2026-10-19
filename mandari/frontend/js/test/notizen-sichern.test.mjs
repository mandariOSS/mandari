/**
 * Tests für das Speichern aller Notiz-Editoren vor einem Neuaufbau (`frontend/alpine/faction-session.ts`, Issue #1071):
 * Das Protokoll nach der Sitzung hat einen Editor je TOP. Ein Neuladen des Dokuments (agenda-refresh) darf erst
 * starten, wenn ALLE Editoren gespeichert haben – sonst lädt es den alten Stand, und das Speichern danach endet im
 * Konflikt mit doppeltem Text.
 *
 * Ausführen mit: npm run test:speichern
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const ziel = join(here, 'build', 'faction-session.cjs')
execSync(
  `npx esbuild frontend/alpine/faction-session.ts --bundle --format=cjs --platform=node --outfile="${ziel}"`,
  { cwd: projectRoot, stdio: 'inherit' },
)

const lauscher = []
globalThis.window = globalThis
globalThis.document = { body: { addEventListener: (art, fn) => lauscher.push([art, fn]) } }

const require = createRequire(import.meta.url)
const { notizenAnmelden, notizenAbmelden, notizenSichern, vorAnfragenSichern } = require(ziel)

let bestanden = 0
async function pruefe(name, fn) {
  await fn()
  bestanden += 1
  console.log(`  OK   ${name}`)
}

const warte = (ms) => new Promise((r) => setTimeout(r, ms))

function editor(dauer, protokoll, name) {
  return {
    async sichern() {
      await warte(dauer)
      protokoll.push(name)
    },
  }
}

await pruefe('notizenSichern wartet auf alle angemeldeten Editoren', async () => {
  const protokoll = []
  const a = editor(30, protokoll, 'a')
  const b = editor(10, protokoll, 'b')
  notizenAnmelden(a)
  notizenAnmelden(b)
  await notizenSichern()
  assert.deepEqual([...protokoll].sort(), ['a', 'b'])
  notizenAbmelden(a)
  notizenAbmelden(b)
})

await pruefe('Ein Fehler eines Editors hält die anderen nicht auf', async () => {
  const protokoll = []
  const kaputt = { sichern: async () => { throw new Error('Netz') } }
  const gut = editor(5, protokoll, 'gut')
  notizenAnmelden(kaputt)
  notizenAnmelden(gut)
  await notizenSichern()
  assert.deepEqual(protokoll, ['gut'])
  notizenAbmelden(kaputt)
  notizenAbmelden(gut)
})

await pruefe('Neuladen (data-sichern-vorher) startet erst nach dem Speichern aller Editoren, Lauscher nur einmal', async () => {
  vorAnfragenSichern()
  vorAnfragenSichern()
  const bestaetigen = lauscher.filter(([art]) => art === 'htmx:confirm')
  assert.equal(bestaetigen.length, 1)
  const protokoll = []
  const a = editor(20, protokoll, 'a')
  const b = editor(40, protokoll, 'b')
  notizenAnmelden(a)
  notizenAnmelden(b)
  let verhindert = false
  const gesendet = new Promise((resolve) => {
    bestaetigen[0][1]({
      preventDefault: () => {
        verhindert = true
      },
      detail: { elt: { closest: () => ({}) }, issueRequest: () => resolve([...protokoll]) },
    })
  })
  assert.equal(verhindert, true)
  assert.deepEqual((await gesendet).sort(), ['a', 'b'])
  notizenAbmelden(a)
  notizenAbmelden(b)
})

await pruefe('Andere Anfragen bleiben unberührt', async () => {
  const [, fn] = lauscher.find(([art]) => art === 'htmx:confirm')
  let verhindert = false
  fn({ preventDefault: () => { verhindert = true }, detail: { elt: { closest: () => null } } })
  assert.equal(verhindert, false)
})

console.log(`${bestanden} bestanden, 0 fehlgeschlagen`)
