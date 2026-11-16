/**
 * Tests für das Ende der Sitzung einer Editor-Verbindung (`frontend/editor/collaboration.ts`).
 *
 * Schließt der Server mit 4440 (die Sitzung der Verbindung gilt nicht mehr, apps/accounts/websocket.py), verbindet der
 * Provider genau einmal neu: Nach einer erneuten Anmeldung oder einem Passwortwechsel im eigenen Profil hat der Browser
 * schon ein neues Cookie. Kommt auch dieser Versuch nicht zustande, meldet er `session_ended`; die Editoren laden dann
 * die Seite neu. Ein gewöhnlicher Abbruch verbindet wie bisher immer wieder neu.
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
const ziel = join(here, 'build', 'collaboration.cjs')
execSync(
  `npx esbuild frontend/editor/collaboration.ts --bundle --format=cjs --platform=node --log-level=error --outfile="${ziel}"`,
  { cwd: projectRoot, stdio: 'inherit' },
)

// Wartezeiten des Providers: aufgezeichnet und auf wenige Millisekunden verkürzt
const echtesTimeout = globalThis.setTimeout
const wartezeiten = []
globalThis.setTimeout = (fn, ms, ...rest) => {
  wartezeiten.push(ms)
  return echtesTimeout(fn, Math.min(ms ?? 0, 5), ...rest)
}
const warte = (ms) => new Promise((fertig) => echtesTimeout(fertig, ms))

// Kein `document`: Die Editor-Bibliotheken fragen beim Laden den Browser ab, sobald es eines gibt. Der Provider braucht
// es nur für das selbstständige Speichern (autoSave), das hier aus ist.
let neuGeladen = 0
globalThis.window = globalThis
globalThis.location = { reload: () => (neuGeladen += 1) }

class Verbindung {
  static OPEN = 1
  static alle = []

  constructor(url) {
    this.url = url
    this.readyState = 0
    this.gesendet = []
    Verbindung.alle.push(this)
  }

  send(daten) {
    this.gesendet.push(daten)
  }

  close() {
    this.readyState = 3
  }

  // Was der Server tut
  annehmen() {
    this.readyState = 1
    this.onopen?.()
  }

  beenden(code) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}
// Neuere Node-Versionen bringen selbst ein WebSocket mit: hier zählt nur die nachgebildete Verbindung
Object.defineProperty(globalThis, 'WebSocket', { value: Verbindung, configurable: true, writable: true })

const require = createRequire(import.meta.url)
const { initCollaboration } = require(ziel)

const SITZUNG_BEENDET = 4440
const ABBRUCH = 1006

function editor(mitMeldung = true) {
  Verbindung.alle.length = 0
  wartezeiten.length = 0
  neuGeladen = 0
  const gruende = []
  const status = []
  const collab = initCollaboration({
    wsUrl: 'ws://test/ws/documents/1/',
    user: { name: 'Test', color: '#000000' },
    autoSave: false,
    onStatusChange: (s) => status.push(s),
    onReloadRequired: mitMeldung ? (grund) => gruende.push(grund) : undefined,
  })
  return { collab, gruende, status }
}

let bestanden = 0
async function pruefe(name, fn) {
  await fn()
  bestanden += 1
  console.log(`  OK   ${name}`)
}

await pruefe('Sitzung beendet, neue Sitzung im Browser: ein neuer Versuch, die Verbindung steht wieder', async () => {
  const { collab, gruende, status } = editor()
  Verbindung.alle[0].annehmen()
  Verbindung.alle[0].beenden(SITZUNG_BEENDET)
  assert.deepEqual(wartezeiten, [2000], 'der neue Versuch wartet auf die Antwort mit dem neuen Cookie')
  await warte(30)
  assert.equal(Verbindung.alle.length, 2)
  Verbindung.alle[1].annehmen()
  await warte(30)
  assert.deepEqual(gruende, [])
  assert.equal(status.at(-1), 'connected')
  assert.equal(Verbindung.alle.length, 2)

  // Danach zählt ein gewöhnlicher Abbruch wieder als gewöhnlicher Abbruch
  Verbindung.alle[1].beenden(ABBRUCH)
  await warte(30)
  assert.equal(Verbindung.alle.length, 3)
  assert.deepEqual(gruende, [])
  collab.destroy()
})

await pruefe('Sitzung beendet, keine neue: nach dem einen Versuch meldet der Provider session_ended', async () => {
  const { collab, gruende } = editor()
  Verbindung.alle[0].annehmen()
  Verbindung.alle[0].beenden(SITZUNG_BEENDET)
  await warte(30)
  assert.equal(Verbindung.alle.length, 2)
  // Abgelehnt vor der Annahme: Der Browser meldet nur den Abbruch
  Verbindung.alle[1].beenden(ABBRUCH)
  await warte(60)
  assert.deepEqual(gruende, ['session_ended'])
  assert.equal(Verbindung.alle.length, 2, 'kein weiterer Versuch')
  assert.equal(await collab.flush(50), false, 'nichts mehr sichern, die Seite lädt neu')
  collab.destroy()
})

await pruefe('Ohne eigenen Umgang mit der Meldung lädt die Seite neu', async () => {
  const { collab } = editor(false)
  Verbindung.alle[0].annehmen()
  Verbindung.alle[0].beenden(SITZUNG_BEENDET)
  await warte(30)
  Verbindung.alle[1].beenden(ABBRUCH)
  await warte(30)
  assert.equal(neuGeladen, 1)
  collab.destroy()
})

await pruefe('Gewöhnlicher Abbruch: immer wieder neu verbinden, keine Meldung', async () => {
  const { collab, gruende } = editor()
  Verbindung.alle[0].annehmen()
  Verbindung.alle[0].beenden(ABBRUCH)
  await warte(30)
  Verbindung.alle[1].beenden(ABBRUCH)
  await warte(30)
  Verbindung.alle[2].beenden(ABBRUCH)
  await warte(30)
  assert.equal(Verbindung.alle.length, 4)
  assert.equal(wartezeiten.length, 3)
  assert.ok(wartezeiten[0] >= 1000 && wartezeiten[0] < 1500, 'erste Wartezeit des gewöhnlichen Wiederverbindens')
  assert.ok(wartezeiten[2] > wartezeiten[0], 'die Wartezeit wächst mit jedem Versuch')
  assert.deepEqual(gruende, [])
  assert.equal(neuGeladen, 0)
  collab.destroy()
})

await pruefe('Nach dem Abbau des Editors gibt es keinen neuen Versuch', async () => {
  const { collab, gruende } = editor()
  const erste = Verbindung.alle[0]
  erste.annehmen()
  collab.destroy()
  erste.beenden(SITZUNG_BEENDET)
  await warte(30)
  assert.equal(Verbindung.alle.length, 1)
  assert.deepEqual(gruende, [])
})

console.log(`\n${bestanden} Tests bestanden`)
process.exit(0)
