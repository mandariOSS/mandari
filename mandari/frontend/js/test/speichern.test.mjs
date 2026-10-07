/**
 * Tests für das Speichern mit Wiederholung (`frontend/js/speichern.ts`, #854).
 *
 * Ausführen mit: npm run test:speichern
 * (baut speichern.ts via esbuild nach CJS; fetch wird je Test nachgebildet, Wartezeiten sind verkürzt)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'speichern.cjs')

execSync(`npx esbuild frontend/js/speichern.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`, {
  cwd: projectRoot,
  stdio: 'inherit',
})

// Meta-Tag mit altem Token, Cookie mit aktuellem: gesendet werden muss das aus dem Cookie
globalThis.document = {
  cookie: 'csrftoken=token-aus-cookie',
  querySelector: () => ({ content: 'token-aus-meta' }),
}

const require = createRequire(import.meta.url)
const { Speicherdienst } = require(outFile)

const pause = (ms) => new Promise((r) => setTimeout(r, ms))

function json(daten, status = 200) {
  return new Response(JSON.stringify(daten), { status, headers: { 'Content-Type': 'application/json' } })
}

function html(status = 200, { redirected = false } = {}) {
  const resp = new Response('<html>Anmelden</html>', { status, headers: { 'Content-Type': 'text/html' } })
  if (redirected) Object.defineProperty(resp, 'redirected', { value: true })
  return resp
}

/** fetch-Nachbildung: Antworten der Reihe nach (Funktion, Response oder Error); Aufrufe werden protokolliert */
function nachgebildet(antworten) {
  const aufrufe = []
  const abrufen = async (url, opts) => {
    aufrufe.push({ url, opts, body: opts.body ? JSON.parse(opts.body) : undefined })
    const wiederverwendet = antworten.length === 1
    const naechste = wiederverwendet ? antworten[0] : antworten.shift()
    const a = typeof naechste === 'function' ? await naechste() : naechste
    if (a instanceof Error) throw a
    // Die letzte Antwort gilt für alle weiteren Aufrufe; ein Body lässt sich nur einmal lesen
    return wiederverwendet && typeof naechste !== 'function' ? a.clone() : a
  }
  return { abrufen, aufrufe }
}

function dienst(antworten, extra = {}) {
  const { abrufen, aufrufe } = nachgebildet(antworten)
  const staende = []
  const d = new Speicherdienst({
    fetch: abrufen,
    wartezeitenMs: [10, 20],
    anmeldungWartezeitMs: 30,
    beiAenderung: (s) => staende.push(s),
    ...extra,
  })
  return { d, aufrufe, staende }
}

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

await test('Erfolg: Daten zurück, Stand ohne Fehler, CSRF-Token aus dem Cookie', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { position: 'for' }, schluessel: 'k' })
  assert.equal(e.ok, true)
  assert.deepEqual(e.daten, { success: true })
  assert.equal(aufrufe[0].opts.headers['X-CSRFToken'], 'token-aus-cookie')
  assert.equal(aufrufe[0].opts.headers.Accept, 'application/json')
  const s = d.stand()
  assert.equal(s.sendet, 0)
  assert.equal(s.wiederholt, 0)
  assert.equal(s.fehler, '')
  assert.ok(s.zuletztGespeichert instanceof Date)
  assert.equal(d.mussWarnen(), false)
})

await test('Neustart (502, dann Netzfehler) wird automatisch wiederholt und gespeichert', async () => {
  const { d, aufrufe, staende } = dienst([json({}, 502), new TypeError('Failed to fetch'), json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { position: 'for' }, schluessel: 'k' })
  assert.equal(e.ok, true)
  assert.equal(aufrufe.length, 3)
  assert.ok(staende.some((s) => s.wiederholt === 1), 'Zwischenstand meldet die Wiederholung')
  assert.equal(d.stand().wiederholt, 0)
})

await test('503, 504 und 429 gelten als vorübergehend', async () => {
  for (const status of [503, 504, 429]) {
    const { d, aufrufe } = dienst([json({}, status), json({ success: true })])
    const e = await d.senden({ url: '/x/', body: { a: 1 }, schluessel: 'k' })
    assert.equal(e.ok, true, `Status ${status}`)
    assert.equal(aufrufe.length, 2, `Status ${status}`)
  }
})

await test('Während eine Anfrage läuft: nur der neueste Stand folgt, ältere werden ersetzt', async () => {
  let freigeben
  const erste = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => erste.then(() => json({ success: true })), json({ success: true })])
  const a = d.senden({ url: '/x/', body: { content: 'A' }, schluessel: 'k' })
  const b = d.senden({ url: '/x/', body: { content: 'AB' }, schluessel: 'k' })
  const c = d.senden({ url: '/x/', body: { content: 'ABC' }, schluessel: 'k' })
  assert.equal(aufrufe.length, 1, 'zweite Anfrage wartet auf die erste')
  assert.equal(d.mussWarnen(), true)
  freigeben()
  assert.equal((await a).ok, true)
  assert.deepEqual(await b, { ok: false, ersetzt: true })
  assert.equal((await c).ok, true)
  assert.deepEqual(
    aufrufe.map((x) => x.body.content),
    ['A', 'ABC'],
  )
})

await test('Nach einer Störung ersetzt ein neuer Stand den wartenden (kein alter Stand überschreibt)', async () => {
  const { d, aufrufe } = dienst([json({}, 502), json({ success: true })])
  const alt = d.senden({ url: '/x/', body: { content: 'alt' }, schluessel: 'k' })
  await pause(2)
  assert.equal(d.stand().wiederholt, 1)
  const neu = d.senden({ url: '/x/', body: { content: 'neu' }, schluessel: 'k' })
  assert.deepEqual(await alt, { ok: false, ersetzt: true })
  assert.equal((await neu).ok, true)
  assert.deepEqual(
    aufrufe.map((x) => x.body.content),
    ['alt', 'neu'],
  )
})

await test('Verschiedene Schlüssel laufen unabhängig', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const [a, b] = await Promise.all([
    d.senden({ url: '/p/', body: { position: 'for' }, schluessel: 'p|position' }),
    d.senden({ url: '/p/', body: { outcome: 'accepted' }, schluessel: 'p|outcome' }),
  ])
  assert.equal(a.ok && b.ok, true)
  assert.equal(aufrufe.length, 2)
})

await test('400 wird nicht wiederholt; Meldung nennt das Ziel; erfolgreiches Speichern räumt den Fehler weg', async () => {
  const { d, aufrufe } = dienst([json({ error: 'Ungültige Position' }, 400), json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { position: '?' }, schluessel: 'k', bezeichnung: 'Position zu TOP 2' })
  assert.equal(e.ok, false)
  assert.equal(e.ersetzt, false)
  assert.equal(e.meldung, 'Position zu TOP 2 nicht gespeichert: Ungültige Position')
  assert.equal(aufrufe.length, 1)
  assert.equal(d.stand().fehler, 'Position zu TOP 2 nicht gespeichert: Ungültige Position')
  assert.equal(d.mussWarnen(), false, 'endgültige Fehler halten niemanden auf der Seite')
  await d.senden({ url: '/x/', body: { position: 'for' }, schluessel: 'k' })
  assert.equal(d.stand().fehler, '')
})

await test('404 und 500 werden nicht wiederholt', async () => {
  for (const status of [404, 500]) {
    const { d, aufrufe } = dienst([json({}, status)])
    const e = await d.senden({ url: '/x/', body: { a: 1 }, schluessel: 'k' })
    assert.equal(e.ok, false, `Status ${status}`)
    assert.equal(aufrufe.length, 1, `Status ${status}`)
  }
})

await test('Ohne Schlüssel (Anlegen) wird auch bei Störung nur einmal gesendet', async () => {
  const { d, aufrufe } = dienst([json({}, 502)])
  const e = await d.senden({ url: '/notes/', body: { content: 'Hallo' } })
  assert.equal(e.ok, false)
  assert.match(e.meldung, /keine Verbindung/)
  await pause(40)
  assert.equal(aufrufe.length, 1)
  assert.equal(d.mussWarnen(), false)
})

await test('Abgelaufene Anmeldung (Umleitung auf HTML): Stand bleibt, wird nach neuer Anmeldung gesendet', async () => {
  const { d, aufrufe } = dienst([html(200, { redirected: true }), json({ success: true })], { anmeldungWartezeitMs: 10000 })
  const e = d.senden({ url: '/x/', body: { content: 'Text' }, schluessel: 'k' })
  await pause(2)
  const s = d.stand()
  assert.equal(s.anmeldung, 'abgelaufen')
  assert.equal(s.wiederholt, 1)
  assert.equal(d.mussWarnen(), true)
  d.jetztWiederholen() // z. B. Fokus zurück nach Anmeldung im anderen Tab
  assert.equal((await e).ok, true)
  assert.equal(aufrufe.length, 2)
  assert.equal(d.stand().anmeldung, '')
})

await test('403 two_factor_setup_required: Hinweis mit Ziel, Stand bleibt', async () => {
  const { d } = dienst([json({ error: 'two_factor_setup_required', redirect: '/accounts/2fa/' }, 403)], {
    anmeldungWartezeitMs: 10000,
  })
  void d.senden({ url: '/x/', body: { content: 'Text' }, schluessel: 'k' })
  await pause(2)
  const s = d.stand()
  assert.equal(s.anmeldung, 'zweiter_faktor')
  assert.equal(s.anmeldungZiel, '/accounts/2fa/')
  assert.equal(s.wiederholt, 1)
})

await test('403 als HTML (CSRF nach neuer Anmeldung) gilt als Anmeldeproblem und wird wiederholt', async () => {
  const { d, aufrufe } = dienst([html(403), json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, schluessel: 'k' })
  assert.equal(e.ok, true)
  assert.equal(aufrufe.length, 2)
})

await test('403 mit JSON-Fehler (keine Berechtigung) ist endgültig', async () => {
  const { d, aufrufe } = dienst([json({ error: 'Unauthorized' }, 403)])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, schluessel: 'k', bezeichnung: 'Notiz' })
  assert.equal(e.ok, false)
  assert.equal(e.meldung, 'Notiz nicht gespeichert: Unauthorized')
  assert.equal(aufrufe.length, 1)
})

await test('Beim Verlassen: kleine Anfragen mit keepalive, keine Rückfrage; große ohne keepalive, mit Rückfrage', async () => {
  let freigeben
  const offen = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => offen.then(() => json({ success: true }))])
  d.verbergen = true
  const klein = d.senden({ url: '/x/', body: { content: 'kurz' }, schluessel: 'a' })
  assert.equal(aufrufe[0].opts.keepalive, true)
  assert.equal(d.mussWarnen(), false)
  const gross = d.senden({ url: '/x/', body: { content: 'x'.repeat(70000) }, schluessel: 'b' })
  assert.equal(aufrufe[1].opts.keepalive, false)
  assert.equal(d.mussWarnen(), true)
  freigeben()
  await Promise.all([klein, gross])
  assert.equal(d.mussWarnen(), false)
})

await test('Senden startet synchron (nötig beim Verlassen der Seite)', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const e = d.senden({ url: '/x/', body: { a: 1 }, schluessel: 'k' })
  assert.equal(aufrufe.length, 1)
  await e
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
// Offene Wiederholungs-Timer (Anmeldeproblem-Tests) würden den Prozess sonst am Leben halten
process.exit(failed > 0 ? 1 : 0)
