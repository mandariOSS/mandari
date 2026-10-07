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

const S = { wiederholbar: true }

await test('Erfolg: Daten zurück, Stand ohne Fehler, CSRF-Token aus dem Cookie', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { position: 'for' }, ...S })
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
  const e = await d.senden({ url: '/x/', body: { position: 'for' }, ...S })
  assert.equal(e.ok, true)
  assert.equal(aufrufe.length, 3)
  assert.ok(
    staende.some((s) => s.wiederholt === 1),
    'Zwischenstand meldet die Wiederholung',
  )
  assert.equal(d.stand().wiederholt, 0)
})

await test('503, 504 und 429 gelten als vorübergehend', async () => {
  for (const status of [503, 504, 429]) {
    const { d, aufrufe } = dienst([json({}, status), json({ success: true })])
    const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S })
    assert.equal(e.ok, true, `Status ${status}`)
    assert.equal(aufrufe.length, 2, `Status ${status}`)
  }
})

await test('500 wird bei Aktualisierungen bis zu dreimal versucht, dann endgültig gemeldet', async () => {
  const { d, aufrufe } = dienst([json({}, 500)])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S, bezeichnung: 'Notiz' })
  assert.equal(e.ok, false)
  assert.equal(e.meldung, 'Notiz nicht gespeichert: Fehler auf dem Server')
  assert.equal(aufrufe.length, 3)
  assert.equal(d.mussWarnen(), true, 'Eingabe ist nicht gespeichert: beim Verlassen nachfragen')
})

await test('Während eine Anfrage läuft: wartende Stände werden zusammengeführt, der neueste gewinnt', async () => {
  let freigeben
  const erste = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => erste.then(() => json({ success: true })), json({ success: true })])
  const a = d.senden({ url: '/x/', body: { content: 'A' }, ...S })
  const b = d.senden({ url: '/x/', body: { content: 'AB' }, ...S })
  const c = d.senden({ url: '/x/', body: { content: 'ABC' }, ...S })
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

await test('Nach einer Störung übernimmt ein neuer Stand den wartenden (kein alter Stand überschreibt)', async () => {
  const { d, aufrufe } = dienst([json({}, 502), json({ success: true })])
  const alt = d.senden({ url: '/x/', body: { content: 'alt' }, ...S })
  await pause(2)
  assert.equal(d.stand().wiederholt, 1)
  const neu = d.senden({ url: '/x/', body: { content: 'neu' }, ...S })
  assert.deepEqual(await alt, { ok: false, ersetzt: true })
  assert.equal((await neu).ok, true)
  assert.deepEqual(
    aufrufe.map((x) => x.body.content),
    ['alt', 'neu'],
  )
})

await test('Verschiedene Felder derselben Adresse laufen nacheinander, nie gleichzeitig (B1)', async () => {
  let freigeben
  const erste = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => erste.then(() => json({ success: true })), json({ success: true })])
  const p = d.senden({ url: '/pos/', body: { position: 'for' }, ...S })
  const o = d.senden({ url: '/pos/', body: { outcome: 'accepted' }, ...S })
  const r = d.senden({ url: '/pos/', body: { reasoning: 'X' }, ...S })
  assert.equal(aufrufe.length, 1)
  freigeben()
  await Promise.all([p, o, r])
  assert.deepEqual(
    aufrufe.map((x) => x.body),
    [{ position: 'for' }, { outcome: 'accepted', reasoning: 'X' }],
  )
})

await test('Scheitert die Position, wird sie mit der wartenden Begründung zusammen wiederholt (B1)', async () => {
  let antwort
  const erste = new Promise((r) => {
    antwort = r
  })
  const { d, aufrufe } = dienst([() => erste, json({ success: true })])
  const p = d.senden({ url: '/pos/', body: { position: 'for' }, ...S })
  const r = d.senden({ url: '/pos/', body: { reasoning: 'X' }, ...S })
  antwort(json({}, 502))
  assert.deepEqual(await p, { ok: false, ersetzt: true })
  assert.equal((await r).ok, true)
  assert.deepEqual(
    aufrufe.map((x) => x.body),
    [{ position: 'for' }, { position: 'for', reasoning: 'X' }],
  )
})

await test('Löschen wartet hinter der Wiederholung desselben Redebeitrags, nichts entsteht neu (B2)', async () => {
  const { d, aufrufe } = dienst([json({}, 502), json({ success: true })])
  const inhalt = d.senden({ url: '/speech/', body: { content: 'alt' }, ...S })
  await pause(2)
  const loeschen = d.senden({ url: '/speech/', method: 'DELETE' })
  assert.equal(aufrufe.length, 1, 'Löschen wartet, bis der Inhalt gespeichert ist')
  assert.equal((await inhalt).ok, true)
  assert.equal((await loeschen).ok, true)
  assert.deepEqual(
    aufrufe.map((x) => x.opts.method),
    ['POST', 'POST', 'DELETE'],
  )
})

await test('Verschiedene Adressen laufen unabhängig', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const [a, b] = await Promise.all([
    d.senden({ url: '/top1/', body: { position: 'for' }, ...S }),
    d.senden({ url: '/top2/', body: { position: 'against' }, ...S }),
  ])
  assert.equal(a.ok && b.ok, true)
  assert.equal(aufrufe.length, 2)
})

await test('400 wird nicht wiederholt; Meldung nennt das Ziel; erfolgreiches Speichern räumt den Fehler weg', async () => {
  const { d, aufrufe } = dienst([json({ error: 'Ungültige Position' }, 400), json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { position: '?' }, ...S, bezeichnung: 'Position zu TOP 2' })
  assert.equal(e.ok, false)
  assert.equal(e.ersetzt, false)
  assert.equal(e.meldung, 'Position zu TOP 2 nicht gespeichert: Ungültige Position')
  assert.equal(aufrufe.length, 1)
  assert.equal(d.stand().fehler, 'Position zu TOP 2 nicht gespeichert: Ungültige Position')
  await d.senden({ url: '/x/', body: { position: 'for' }, ...S })
  assert.equal(d.stand().fehler, '')
  assert.equal(d.mussWarnen(), false)
})

await test('Angezeigt wird die jüngste Fehlermeldung (B7)', async () => {
  const { d } = dienst([json({ error: 'kaputt' }, 400)])
  await d.senden({ url: '/p/', body: { a: 1 }, ...S, bezeichnung: 'P' })
  await d.senden({ url: '/n/', body: { a: 1 }, ...S, bezeichnung: 'N' })
  await d.senden({ url: '/p/', body: { a: 2 }, ...S, bezeichnung: 'P' })
  assert.equal(d.stand().fehler, 'P nicht gespeichert: kaputt')
})

await test('404 wird nicht wiederholt', async () => {
  const { d, aufrufe } = dienst([json({}, 404)])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S })
  assert.equal(e.ok, false)
  assert.equal(aufrufe.length, 1)
})

await test('Einmal-Aufträge (Anlegen) werden auch bei Störung oder 500 nur einmal gesendet', async () => {
  for (const antwort of [json({}, 502), json({}, 500)]) {
    const { d, aufrufe } = dienst([antwort])
    const e = await d.senden({ url: '/notes/', body: { content: 'Hallo' } })
    assert.equal(e.ok, false)
    await pause(40)
    assert.equal(aufrufe.length, 1)
    assert.equal(d.mussWarnen(), false)
  }
})

await test('Abgelaufene Anmeldung (Umleitung auf HTML): Stand bleibt, wird nach neuer Anmeldung gesendet', async () => {
  const { d, aufrufe } = dienst([html(200, { redirected: true }), json({ success: true })], {
    anmeldungWartezeitMs: 10000,
  })
  const e = d.senden({ url: '/x/', body: { content: 'Text' }, ...S })
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

await test('Nach erfolgreicher Anmeldung werden auch die übrigen wartenden Adressen sofort gesendet (B7)', async () => {
  const antworten = new Map([
    ['/a/', [html(200, { redirected: true }), json({ success: true })]],
    ['/b/', [json({ success: true })]],
  ])
  const aufrufe = []
  const d = new Speicherdienst({
    fetch: async (url) => {
      aufrufe.push(url)
      return antworten.get(url).shift()
    },
    wartezeitenMs: [10],
    anmeldungWartezeitMs: 10000,
  })
  const a = d.senden({ url: '/a/', body: { x: 1 }, ...S })
  await pause(2)
  assert.equal(d.stand().anmeldung, 'abgelaufen')
  await d.senden({ url: '/b/', body: { x: 1 }, ...S })
  assert.equal((await a).ok, true, 'ohne 10 s auf den Timer zu warten')
  assert.deepEqual(aufrufe, ['/a/', '/b/', '/a/'])
})

await test('403 two_factor_setup_required: Hinweis mit Ziel, Stand bleibt', async () => {
  const { d } = dienst([json({ error: 'two_factor_setup_required', redirect: '/accounts/2fa/' }, 403)], {
    anmeldungWartezeitMs: 10000,
  })
  void d.senden({ url: '/x/', body: { content: 'Text' }, ...S })
  await pause(2)
  const s = d.stand()
  assert.equal(s.anmeldung, 'zweiter_faktor')
  assert.equal(s.anmeldungZiel, '/accounts/2fa/')
  assert.equal(s.wiederholt, 1)
})

await test('403 als HTML (z. B. Recht entzogen) ist endgültig, keine Dauerschleife (B4)', async () => {
  const { d, aufrufe } = dienst([html(403)])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S, bezeichnung: 'Notiz' })
  assert.equal(e.ok, false)
  assert.equal(e.meldung, 'Notiz nicht gespeichert: Zugriff verweigert')
  await pause(40)
  assert.equal(aufrufe.length, 1)
  assert.equal(d.stand().anmeldung, '')
})

await test('403 mit JSON-Fehler (keine Berechtigung) ist endgültig', async () => {
  const { d, aufrufe } = dienst([json({ error: 'Unauthorized' }, 403)])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S, bezeichnung: 'Notiz' })
  assert.equal(e.ok, false)
  assert.equal(e.meldung, 'Notiz nicht gespeichert: Unauthorized')
  assert.equal(aufrufe.length, 1)
})

await test('Beim Verlassen: kleine Anfragen mit keepalive, keine Rückfrage; große ohne, mit Rückfrage', async () => {
  let freigeben
  const offen = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => offen.then(() => json({ success: true }))])
  d.verbergen = true
  const klein = d.senden({ url: '/a/', body: { content: 'kurz' }, ...S })
  assert.equal(aufrufe[0].opts.keepalive, true)
  assert.equal(d.mussWarnen(), false)
  const gross = d.senden({ url: '/b/', body: { content: 'x'.repeat(70000) }, ...S })
  assert.equal(aufrufe[1].opts.keepalive, false)
  assert.equal(d.mussWarnen(), true)
  freigeben()
  await Promise.all([klein, gross])
  assert.equal(d.mussWarnen(), false)
})

await test('keepalive: Grenze in Bytes über alle laufenden Anfragen (B6)', async () => {
  let freigeben
  const offen = new Promise((r) => {
    freigeben = r
  })
  const { d, aufrufe } = dienst([() => offen.then(() => json({ success: true }))])
  d.verbergen = true
  // 20.000 Zeichen „ä“ = 40.000 Bytes: die erste passt, die zweite nicht mehr dazu
  const a = d.senden({ url: '/a/', body: { content: 'ä'.repeat(20000) }, ...S })
  const b = d.senden({ url: '/b/', body: { content: 'ä'.repeat(20000) }, ...S })
  assert.equal(aufrufe[0].opts.keepalive, true)
  assert.equal(aufrufe[1].opts.keepalive, false)
  freigeben()
  await Promise.all([a, b])
  // Nach dem Ende ist das Kontingent wieder frei
  await d.senden({ url: '/c/', body: { content: 'ä'.repeat(20000) }, ...S })
  assert.equal(aufrufe[2].opts.keepalive, true)
})

await test('Gespeicherte Position räumt die verlorene Begründung nicht weg (N1)', async () => {
  const { d } = dienst([json({}, 500), json({}, 500), json({}, 500), json({ success: true })])
  const r = await d.senden({ url: '/pos/', body: { reasoning: 'X' }, ...S, bezeichnung: 'Begründung' })
  assert.equal(r.ok, false)
  await d.senden({ url: '/pos/', body: { position: 'for' }, ...S })
  assert.equal(d.stand().fehler, 'Begründung nicht gespeichert: Fehler auf dem Server')
  assert.equal(d.mussWarnen(), true)
  await d.senden({ url: '/pos/', body: { reasoning: 'X' }, ...S })
  assert.equal(d.stand().fehler, '')
  assert.equal(d.mussWarnen(), false)
})

await test('Löschen räumt verlorene Felder desselben Ziels weg', async () => {
  const { d } = dienst([json({ error: 'kaputt' }, 400), json({ success: true })])
  await d.senden({ url: '/speech/', body: { content: 'X' }, ...S })
  assert.equal(d.mussWarnen(), true)
  await d.senden({ url: '/speech/', method: 'DELETE' })
  assert.equal(d.mussWarnen(), false)
})

await test('Störungen zählen nicht zur Grenze für 500 (N2: 502, 502, 500, dann ok)', async () => {
  const { d, aufrufe } = dienst([json({}, 502), json({}, 502), json({}, 500), json({ success: true })])
  const e = await d.senden({ url: '/x/', body: { a: 1 }, ...S })
  assert.equal(e.ok, true)
  assert.equal(aufrufe.length, 4)
})

await test('Zeitlimit auch für Einmal-Aufträge: hängendes Löschen gibt die Reihe frei (N5)', async () => {
  const aufrufe = []
  let zaehler = 0
  const d = new Speicherdienst({
    zeitlimitMs: 20,
    wartezeitenMs: [10],
    fetch: (url, opts) => {
      aufrufe.push(opts.method)
      zaehler++
      if (zaehler === 1) {
        // erste Anfrage hängt, bis das Zeitlimit sie abbricht
        return new Promise((_, ablehnen) => {
          opts.signal.addEventListener('abort', () => ablehnen(new DOMException('Zeitlimit', 'TimeoutError')))
        })
      }
      return Promise.resolve(json({ success: true }))
    },
  })
  const loeschen = d.senden({ url: '/speech/', method: 'DELETE' })
  const inhalt = d.senden({ url: '/speech/', body: { content: 'neu' }, ...S })
  const e = await loeschen
  assert.equal(e.ok, false)
  assert.match(e.meldung, /keine Verbindung/)
  assert.equal((await inhalt).ok, true)
  assert.deepEqual(aufrufe, ['DELETE', 'POST'])
})

await test('Meldung nennt alle Felder einer zusammengeführten Aktualisierung', async () => {
  let antwort
  const erste = new Promise((r) => {
    antwort = r
  })
  const { d } = dienst([() => erste, json({ error: 'kaputt' }, 400)])
  const benenne = (body) => `${Object.keys(body).join(', ')} zu TOP 2`
  void d.senden({ url: '/pos/', body: { position: 'for' }, ...S, bezeichnung: benenne })
  const r = d.senden({ url: '/pos/', body: { reasoning: 'X' }, ...S, bezeichnung: benenne })
  antwort(json({}, 502))
  const e = await r
  assert.equal(e.meldung, 'position, reasoning zu TOP 2 nicht gespeichert: kaputt')
})

await test('Senden startet synchron (nötig beim Verlassen der Seite)', async () => {
  const { d, aufrufe } = dienst([json({ success: true })])
  const e = d.senden({ url: '/x/', body: { a: 1 }, ...S })
  assert.equal(aufrufe.length, 1)
  await e
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
// Offene Wiederholungs-Timer (Anmeldeproblem-Tests) würden den Prozess sonst am Leben halten
process.exit(failed > 0 ? 1 : 0)
