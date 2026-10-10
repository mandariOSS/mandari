/**
 * Tests für die Sicherung ungespeicherter Eingaben im Browser (`frontend/js/eingaben-sicherung.ts`, #854 Teil 2),
 * allein und zusammen mit dem Speicherdienst (`frontend/js/speichern.ts`).
 *
 * Ausführen mit: npm run test:speichern
 * (baut beide Module via esbuild nach CJS; localStorage, Uhr und fetch werden je Test nachgebildet)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const build = (modul) => {
  const ziel = join(here, 'build', `${modul}.cjs`)
  execSync(`npx esbuild frontend/js/${modul}.ts --bundle --format=cjs --platform=node --outfile="${ziel}"`, {
    cwd: projectRoot,
    stdio: 'inherit',
  })
  return ziel
}

globalThis.document = { cookie: 'csrftoken=token', querySelector: () => null }

const require = createRequire(import.meta.url)
const { Eingabensicherung, aufraeumen, alleLoeschen, PRAEFIX, HOECHSTDAUER_MS } = require(build('eingaben-sicherung'))
const { Speicherdienst } = require(build('speichern'))

const KONTO = '8f1c2a4e-0b7d-4c55-9a3e-2d6f1b9e7c10'
const ANDERES_KONTO = '1d2e3f40-5a6b-4c7d-8e9f-0a1b2c3d4e5f'
const SITZUNG = '6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d'
const TOP = 'c0ffee00-1111-4222-8333-944455556666'
const TOP2 = 'decade00-1111-4222-8333-944455556666'

/** localStorage-Nachbildung (Map) */
class Speicher {
  constructor() {
    this.daten = new Map()
  }
  get length() {
    return this.daten.size
  }
  key(i) {
    return [...this.daten.keys()][i] ?? null
  }
  getItem(k) {
    return this.daten.has(k) ? this.daten.get(k) : null
  }
  setItem(k, v) {
    this.daten.set(k, String(v))
  }
  removeItem(k) {
    this.daten.delete(k)
  }
  /** Gesicherte Einträge (ohne andere Einstellungen) */
  sicherungen() {
    return [...this.daten.keys()].filter((k) => k.startsWith(PRAEFIX))
  }
}

const pause = (ms) => new Promise((r) => setTimeout(r, ms))

function json(daten, status = 200) {
  return new Response(JSON.stringify(daten), { status, headers: { 'Content-Type': 'application/json' } })
}

function html(status = 200, { redirected = false } = {}) {
  const resp = new Response('<html>Anmelden</html>', { status, headers: { 'Content-Type': 'text/html' } })
  if (redirected) Object.defineProperty(resp, 'redirected', { value: true })
  return resp
}

/** Speicherdienst mit Sicherung; Antworten der Reihe nach (Funktion, Response oder Error), die letzte gilt weiter */
function aufbau(antworten, { konto = KONTO, speicher = new Speicher(), jetzt } = {}) {
  const sicherung = new Eingabensicherung(konto, SITZUNG, { speicher, ...(jetzt ? { jetzt } : {}) })
  const aufrufe = []
  const d = new Speicherdienst({
    sicherung,
    wartezeitenMs: [10, 20],
    anmeldungWartezeitMs: 10000,
    fetch: async (url, opts) => {
      aufrufe.push({ url, body: opts.body ? JSON.parse(opts.body) : undefined })
      const wiederverwendet = antworten.length === 1
      const naechste = wiederverwendet ? antworten[0] : antworten.shift()
      const a = typeof naechste === 'function' ? await naechste() : naechste
      if (a instanceof Error) throw a
      return wiederverwendet && typeof naechste !== 'function' ? a.clone() : a
    },
  })
  return { d, sicherung, speicher, aufrufe }
}

const notiz = (content) => ({
  url: `/notiz/${TOP}/`,
  body: { content },
  wiederholbar: true,
  sicherung: { top: TOP, bereich: 'notiz' },
})
const position = (felder) => ({
  url: `/position/${TOP}/`,
  body: felder,
  wiederholbar: true,
  sicherung: { top: TOP, bereich: 'position' },
})

let passed = 0
let failed = 0

async function test(name, fn) {
  try {
    await fn()
    passed++
    console.log(`  OK   ${name}`)
  } catch (err) {
    failed++
    console.error(`  FAIL ${name}`)
    console.error(`       ${err.stack || err.message}`)
  }
}

await test('Schlüssel je Konto, Sitzung und TOP mit der Kontokennung, nie dem Namen', async () => {
  const speicher = new Speicher()
  const s = new Eingabensicherung(KONTO, SITZUNG, { speicher, jetzt: () => 1000 })
  s.merken({ top: TOP, bereich: 'notiz' }, { content: 'Rückfrage an die Verwaltung' })
  assert.deepEqual(speicher.sicherungen(), [`mandari.eingaben.${KONTO}.${SITZUNG}.${TOP}`])
  assert.deepEqual(s.lesen(TOP), { zeit: 1000, bereiche: { notiz: { content: 'Rückfrage an die Verwaltung' } } })
  // Kennungen mit Trennzeichen (oder ein Name mit Leerzeichen) sichern nichts
  const ohne = new Eingabensicherung('Erika Mustermann', SITZUNG, { speicher: new Speicher() })
  assert.equal(ohne.aktiv, false)
  const leer = new Eingabensicherung('', SITZUNG, { speicher: new Speicher() })
  leer.merken({ top: TOP, bereich: 'notiz' }, { content: 'x' })
  assert.deepEqual(leer.alle(), [])
})

await test('Erfolgreich gespeichert: Sicherung ist sofort weg', async () => {
  const { d, speicher } = aufbau([json({ success: true })])
  const laufend = d.senden(notiz('Text'))
  assert.equal(speicher.sicherungen().length, 1, 'gesichert, bevor die Antwort da ist')
  assert.equal((await laufend).ok, true)
  assert.deepEqual(speicher.sicherungen(), [])
})

await test('Während des Speicherns weitergetippt: nur der bestätigte Stand verschwindet', async () => {
  let freigeben
  const erste = new Promise((r) => {
    freigeben = r
  })
  let zweiteFreigeben
  const zweite = new Promise((r) => {
    zweiteFreigeben = r
  })
  const { d, sicherung } = aufbau([() => erste.then(() => json({ success: true })), () => zweite])
  const a = d.senden(notiz('Text'))
  const b = d.senden(notiz('Text mit mehr'))
  freigeben()
  assert.equal((await a).ok, true)
  assert.deepEqual(sicherung.lesen(TOP).bereiche, { notiz: { content: 'Text mit mehr' } })
  zweiteFreigeben(json({ success: true }))
  assert.equal((await b).ok, true)
  assert.equal(sicherung.lesen(TOP), null)
})

await test('Nach Störung zusammengeführt (Position und Begründung): beide Felder verschwinden nach dem Speichern', async () => {
  let antwort
  const erste = new Promise((r) => {
    antwort = r
  })
  const { d, sicherung, aufrufe } = aufbau([() => erste, json({ success: true })])
  const p = d.senden(position({ position: 'for' }))
  const r = d.senden(position({ reasoning: 'Der Radweg fehlt.' }))
  antwort(json({}, 502))
  assert.deepEqual(await p, { ok: false, ersetzt: true })
  assert.equal((await r).ok, true)
  assert.deepEqual(aufrufe[1].body, { position: 'for', reasoning: 'Der Radweg fehlt.' })
  assert.equal(sicherung.lesen(TOP), null)
})

await test('Netz aus: Eingabe bleibt im Browser, bis die Wiederholung gelingt', async () => {
  const { d, sicherung, aufrufe } = aufbau([new TypeError('Failed to fetch'), json({ success: true })])
  const e = d.senden(notiz('Offline geschrieben'))
  await pause(2)
  assert.equal(d.stand().wiederholt, 1)
  assert.deepEqual(sicherung.lesen(TOP).bereiche.notiz, { content: 'Offline geschrieben' })
  assert.equal((await e).ok, true)
  assert.equal(aufrufe.length, 2)
  assert.equal(sicherung.lesen(TOP), null)
})

await test('Abgelaufene Anmeldung: Eingabe bleibt im Browser, nach neuer Anmeldung gespeichert und gelöscht', async () => {
  const { d, sicherung } = aufbau([html(200, { redirected: true }), json({ success: true })])
  const e = d.senden(notiz('Vor dem Ablauf geschrieben'))
  await pause(2)
  assert.equal(d.stand().anmeldung, 'abgelaufen')
  assert.deepEqual(sicherung.lesen(TOP).bereiche.notiz, { content: 'Vor dem Ablauf geschrieben' })
  d.jetztWiederholen() // Fokus zurück nach Anmeldung im anderen Tab
  assert.equal((await e).ok, true)
  assert.equal(sicherung.lesen(TOP), null)
})

await test('Antwort 401: Eingabe bleibt im Browser', async () => {
  const { d, sicherung } = aufbau([json({}, 401)])
  void d.senden(notiz('Text'))
  await pause(2)
  assert.equal(d.stand().anmeldung, 'abgelaufen')
  assert.ok(sicherung.lesen(TOP))
})

await test('403 two_factor_setup_required: Hinweis mit Ziel, Eingabe bleibt im Browser', async () => {
  const { d, sicherung } = aufbau([json({ error: 'two_factor_setup_required', redirect: '/accounts/2fa/' }, 403)])
  void d.senden(position({ position: 'against' }))
  await pause(2)
  const s = d.stand()
  assert.equal(s.anmeldung, 'zweiter_faktor')
  assert.equal(s.anmeldungZiel, '/accounts/2fa/')
  assert.deepEqual(sicherung.lesen(TOP).bereiche.position, { position: 'against' })
})

await test('Endgültiger Fehler (400): Eingabe bleibt im Browser', async () => {
  const { d, sicherung } = aufbau([json({ error: 'Ungültige Position' }, 400)])
  const e = await d.senden(position({ position: '?' }))
  assert.equal(e.ok, false)
  assert.ok(sicherung.lesen(TOP))
})

await test('Einmal-Aufträge (Anlegen, Löschen) werden nicht gesichert', async () => {
  const { d, speicher } = aufbau([json({}, 502)])
  await d.senden({ url: '/notes/', body: { content: 'Beitrag' }, sicherung: { top: TOP, bereich: 'x' } })
  assert.deepEqual(speicher.sicherungen(), [])
})

await test('Höchstens 24 Stunden: Älteres wird weder angeboten noch behalten', async () => {
  let uhr = 0
  const speicher = new Speicher()
  const s = new Eingabensicherung(KONTO, SITZUNG, { speicher, jetzt: () => uhr })
  s.merken({ top: TOP, bereich: 'notiz' }, { content: 'alt' })
  uhr = HOECHSTDAUER_MS - 1
  s.merken({ top: TOP2, bereich: 'notiz' }, { content: 'neu' })
  assert.equal(s.alle().length, 2, 'knapp 24 Stunden: noch da')
  uhr = HOECHSTDAUER_MS + 1
  assert.deepEqual(
    s.alle().map((e) => e.top),
    [TOP2],
  )
  assert.equal(speicher.sicherungen().length, 1, 'abgelaufener Eintrag gelöscht')
  // Jede neue Eingabe zählt die 24 Stunden neu
  s.merken({ top: TOP2, bereich: 'notiz' }, { content: 'neuer' })
  uhr = HOECHSTDAUER_MS * 2
  assert.ok(s.lesen(TOP2))
})

await test('Aufräumen beim Seitenaufruf: abgelaufene und unlesbare Einträge aller Konten', async () => {
  const speicher = new Speicher()
  new Eingabensicherung(KONTO, SITZUNG, { speicher, jetzt: () => 0 }).merken(
    { top: TOP, bereich: 'notiz' },
    { content: 'alt' },
  )
  new Eingabensicherung(ANDERES_KONTO, SITZUNG, { speicher, jetzt: () => HOECHSTDAUER_MS }).merken(
    { top: TOP, bereich: 'notiz' },
    { content: 'frisch' },
  )
  speicher.setItem(`${PRAEFIX}kaputt`, '{nicht json')
  speicher.setItem('mandari.vorbereitung.zoom', '4')
  aufraeumen(speicher, HOECHSTDAUER_MS + 10)
  assert.deepEqual(speicher.sicherungen(), [`${PRAEFIX}${ANDERES_KONTO}.${SITZUNG}.${TOP}`])
  assert.equal(speicher.getItem('mandari.vorbereitung.zoom'), '4')
})

await test('Je Konto getrennt: ein anderes Konto sieht die Eingaben nicht', async () => {
  const speicher = new Speicher()
  new Eingabensicherung(KONTO, SITZUNG, { speicher }).merken({ top: TOP, bereich: 'notiz' }, { content: 'privat' })
  const anderes = new Eingabensicherung(ANDERES_KONTO, SITZUNG, { speicher })
  assert.deepEqual(anderes.alle(), [])
  assert.equal(anderes.lesen(TOP), null)
  // Auch eine andere Sitzung desselben Kontos ist getrennt
  assert.deepEqual(new Eingabensicherung(KONTO, TOP2, { speicher }).alle(), [])
})

await test('Abmelden löscht die Sicherungen aller Konten, andere Einstellungen bleiben', async () => {
  const speicher = new Speicher()
  new Eingabensicherung(KONTO, SITZUNG, { speicher }).merken({ top: TOP, bereich: 'notiz' }, { content: 'a' })
  new Eingabensicherung(ANDERES_KONTO, SITZUNG, { speicher }).merken({ top: TOP, bereich: 'notiz' }, { content: 'b' })
  speicher.setItem('darkMode', 'true')
  alleLoeschen(speicher)
  assert.deepEqual(speicher.sicherungen(), [])
  assert.equal(speicher.getItem('darkMode'), 'true')
})

await test('Vergessen nur bei gleichem Wert; verwerfen löscht einen ganzen Bereich', async () => {
  const speicher = new Speicher()
  const s = new Eingabensicherung(KONTO, SITZUNG, { speicher })
  s.merken({ top: TOP, bereich: 'rede' }, { content: '<p>Rede</p>', estimated_duration: 90 })
  s.merken({ top: TOP, bereich: 'notiz' }, { content: 'Notiz' })
  s.vergessen({ top: TOP, bereich: 'rede' }, { content: '<p>anders</p>', estimated_duration: 90 })
  assert.deepEqual(s.lesen(TOP).bereiche.rede, { content: '<p>Rede</p>' })
  s.verwerfen({ top: TOP, bereich: 'rede' })
  assert.deepEqual(s.lesen(TOP).bereiche, { notiz: { content: 'Notiz' } })
  s.entfernen(TOP)
  assert.equal(s.lesen(TOP), null)
})

await test('Ohne nutzbaren localStorage (voll, gesperrt): Speichern läuft ohne Sicherung weiter', async () => {
  const voll = new Speicher()
  voll.setItem = () => {
    throw new DOMException('voll', 'QuotaExceededError')
  }
  const { d } = aufbau([json({ success: true })], { speicher: voll })
  assert.equal((await d.senden(notiz('Text'))).ok, true)
  const gesperrt = new Eingabensicherung(KONTO, SITZUNG, { speicher: null })
  gesperrt.merken({ top: TOP, bereich: 'notiz' }, { content: 'x' })
  assert.deepEqual(gesperrt.alle(), [])
})

await test('Nur einfache Werte werden gesichert', async () => {
  const s = new Eingabensicherung(KONTO, SITZUNG, { speicher: new Speicher() })
  s.merken({ top: TOP, bereich: 'rede' }, { linked_document: null, liste: [1], is_shared: true })
  assert.deepEqual(s.lesen(TOP).bereiche, { rede: { is_shared: true } })
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
// Offene Wiederholungs-Timer (Anmeldeproblem-Tests) würden den Prozess sonst am Leben halten
process.exit(failed > 0 ? 1 : 0)
