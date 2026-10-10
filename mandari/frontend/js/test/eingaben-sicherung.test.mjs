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
const {
  ABGEMELDET,
  Eingabensicherung,
  HOECHSTDAUER_MS,
  PRAEFIX,
  alleLoeschen,
  aufraeumen,
  beiAbmeldungSperren,
  uebernehmbar,
  vergleichen,
} = require(build('eingaben-sicherung'))
const { Speicherdienst } = require(build('speichern'))

const KONTO = '8f1c2a4e-0b7d-4c55-9a3e-2d6f1b9e7c10'
const ANDERES_KONTO = '1d2e3f40-5a6b-4c7d-8e9f-0a1b2c3d4e5f'
const ORG = '0a0b0c0d-1111-4222-8333-444455556666'
const ANDERE_ORG = '9f9e9d9c-1111-4222-8333-444455556666'
const SITZUNG = '6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d'
const TOP = 'c0ffee00-1111-4222-8333-944455556666'
const TOP2 = 'decade00-1111-4222-8333-944455556666'
const NOTIZ = { top: TOP, bereich: 'notiz' }
const POSITION = { top: TOP, bereich: 'position' }

/** localStorage-Nachbildung (Map), zählt Schreibvorgänge */
class Speicher {
  constructor() {
    this.daten = new Map()
    this.geschrieben = 0
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
    this.geschrieben++
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

/** Gesicherte Werte eines Eintrags ohne Zeit und Grundlage: `{ bereich: { feld: wert } }` */
function werte(eintrag) {
  if (!eintrag) return null
  return Object.fromEntries(
    Object.entries(eintrag.bereiche).map(([bereich, felder]) => [
      bereich,
      Object.fromEntries(Object.entries(felder).map(([feld, gesichert]) => [feld, gesichert.wert])),
    ]),
  )
}

const sicherung = (optionen = {}, konto = KONTO, org = ORG, sitzung = SITZUNG) =>
  new Eingabensicherung(konto, org, sitzung, { speicher: new Speicher(), ...optionen })

function json(daten, status = 200) {
  return new Response(JSON.stringify(daten), { status, headers: { 'Content-Type': 'application/json' } })
}

function html(status = 200, { redirected = false } = {}) {
  const resp = new Response('<html>Anmelden</html>', { status, headers: { 'Content-Type': 'text/html' } })
  if (redirected) Object.defineProperty(resp, 'redirected', { value: true })
  return resp
}

/** Speicherdienst mit Sicherung; Antworten der Reihe nach (Funktion, Response oder Error), die letzte gilt weiter */
function aufbau(antworten, { konto = KONTO, speicher = new Speicher(), jetzt, verzoegerungMs } = {}) {
  const optionen = { speicher, ...(jetzt ? { jetzt } : {}), ...(verzoegerungMs ? { verzoegerungMs } : {}) }
  const s = new Eingabensicherung(konto, ORG, SITZUNG, optionen)
  const aufrufe = []
  const d = new Speicherdienst({
    sicherung: s,
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
  return { d, sicherung: s, speicher, aufrufe }
}

const notiz = (content) => ({ url: `/notiz/${TOP}/`, body: { content }, wiederholbar: true, sicherung: NOTIZ })
const position = (felder) => ({ url: `/position/${TOP}/`, body: felder, wiederholbar: true, sicherung: POSITION })

/** Antwort, die erst auf Zuruf kommt */
function zurueckgehalten() {
  let freigeben
  const antwort = new Promise((r) => {
    freigeben = r
  })
  return { antwort: () => antwort, freigeben: (a) => freigeben(a) }
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

// ---------- Schlüssel: Konto, Organisation, Sitzung, TOP ----------

await test('Schlüssel je Konto, Organisation, Sitzung und TOP mit Kennungen, nie Namen', async () => {
  const speicher = new Speicher()
  const s = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher, jetzt: () => 1000 })
  s.merken(NOTIZ, { content: 'Rückfrage an die Verwaltung' })
  s.festschreiben()
  assert.deepEqual(speicher.sicherungen(), [`mandari.eingaben.${KONTO}.${ORG}.${SITZUNG}.${TOP}`])
  assert.deepEqual(s.lesen(TOP), { bereiche: { notiz: { content: { wert: 'Rückfrage an die Verwaltung', zeit: 1000 } } } })
  // Kennungen mit Trennzeichen (oder ein Name mit Leerzeichen) sichern nichts
  assert.equal(sicherung({}, 'Erika Mustermann').aktiv, false)
  assert.equal(sicherung({}, KONTO, 'fraktion.test').aktiv, false)
  const leer = sicherung({}, KONTO, '')
  leer.merken(NOTIZ, { content: 'x' })
  assert.deepEqual(leer.alle(), [])
})

await test('Je Organisation getrennt: dieselbe Sitzung, derselbe TOP, zwei Mitgliedschaften', async () => {
  const speicher = new Speicher()
  const fraktion = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher })
  const gruppe = new Eingabensicherung(KONTO, ANDERE_ORG, SITZUNG, { speicher })
  fraktion.merken(POSITION, { position: 'for', reasoning: 'Vertraulich aus der Fraktion' })
  gruppe.merken(POSITION, { position: 'for' })
  fraktion.festschreiben()
  gruppe.festschreiben()
  assert.equal(speicher.sicherungen().length, 2, 'zwei getrennte Einträge')
  // Die Gruppe sieht die Begründung der Fraktion nicht
  assert.deepEqual(werte(gruppe.lesen(TOP)), { position: { position: 'for' } })
  assert.deepEqual(
    gruppe.alle().map((e) => werte(e.eintrag)),
    [{ position: { position: 'for' } }],
  )
  // Verwerfen bzw. Vergessen in der Gruppe lässt die Fraktion unberührt (auch bei gleichem Wert)
  gruppe.vergessen(POSITION, { position: 'for' })
  gruppe.verwerfen(POSITION)
  assert.equal(gruppe.lesen(TOP), null)
  assert.deepEqual(werte(fraktion.lesen(TOP)), {
    position: { position: 'for', reasoning: 'Vertraulich aus der Fraktion' },
  })
})

// ---------- Klartext nur bei Störung oder beim Verlassen auf der Platte ----------

await test('Sofort bestätigt: nichts im localStorage', async () => {
  const { d, sicherung: s, speicher } = aufbau([json({ success: true })])
  const laufend = d.senden(notiz('Vertrauliche Notiz'))
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Vertrauliche Notiz' } }, 'im Speicher der Seite')
  assert.equal((await laufend).ok, true)
  assert.equal(s.lesen(TOP), null)
  assert.equal(speicher.geschrieben, 0, 'nie in den localStorage geschrieben')
})

await test('Nicht binnen der Frist bestätigt: in den localStorage, nach der Bestätigung wieder weg', async () => {
  const halten = zurueckgehalten()
  const { d, speicher } = aufbau([halten.antwort], { verzoegerungMs: 5 })
  const laufend = d.senden(notiz('Langsamer Server'))
  assert.deepEqual(speicher.sicherungen(), [])
  await pause(30)
  assert.equal(speicher.sicherungen().length, 1)
  halten.freigeben(json({ success: true }))
  assert.equal((await laufend).ok, true)
  assert.deepEqual(speicher.sicherungen(), [])
})

await test('Seite verborgen oder verlassen: festschreiben schreibt sofort', async () => {
  const speicher = new Speicher()
  const t = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher, verzoegerungMs: 100000 })
  t.merken(NOTIZ, { content: 'Beim Schließen noch offen' })
  assert.deepEqual(speicher.sicherungen(), [])
  t.festschreiben()
  assert.equal(speicher.sicherungen().length, 1)
  // Liegt der Eintrag schon im localStorage, gehen weitere Eingaben gleich dorthin
  t.merken(NOTIZ, { content: 'Beim Schließen noch offen, ergänzt' })
  assert.match(speicher.getItem(speicher.sicherungen()[0]), /ergänzt/)
})

// ---------- Mit dem Speicherdienst ----------

await test('Während des Speicherns weitergetippt: nur der bestätigte Stand verschwindet, ohne Konflikt', async () => {
  const erste = zurueckgehalten()
  const zweite = zurueckgehalten()
  const { d, sicherung: s } = aufbau([erste.antwort, zweite.antwort])
  s.serverstandSetzen(NOTIZ, { content: '' })
  const a = d.senden(notiz('Text'))
  const b = d.senden(notiz('Text mit mehr'))
  assert.equal(s.lesen(TOP).bereiche.notiz.content.vorher, '')
  erste.freigeben(json({ success: true }))
  assert.equal((await a).ok, true)
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Text mit mehr' } })
  // Die neuere Eingabe beruht jetzt auf dem bestätigten Stand: beim Neuladen kein Konflikt
  assert.equal(s.lesen(TOP).bereiche.notiz.content.vorher, 'Text')
  zweite.freigeben(json({ success: true }))
  assert.equal((await b).ok, true)
  assert.equal(s.lesen(TOP), null)
})

await test('Nach Störung zusammengeführt (Position und Begründung): beide Felder verschwinden nach dem Speichern', async () => {
  const erste = zurueckgehalten()
  const { d, sicherung: s, aufrufe } = aufbau([erste.antwort, json({ success: true })])
  const p = d.senden(position({ position: 'for' }))
  const r = d.senden(position({ reasoning: 'Der Radweg fehlt.' }))
  erste.freigeben(json({}, 502))
  assert.deepEqual(await p, { ok: false, ersetzt: true })
  assert.equal((await r).ok, true)
  assert.deepEqual(aufrufe[1].body, { position: 'for', reasoning: 'Der Radweg fehlt.' })
  assert.equal(s.lesen(TOP), null)
})

await test('Netz aus: Eingabe bleibt gesichert, bis die Wiederholung gelingt', async () => {
  const { d, sicherung: s, aufrufe } = aufbau([new TypeError('Failed to fetch'), json({ success: true })])
  const e = d.senden(notiz('Offline geschrieben'))
  await pause(2)
  assert.equal(d.stand().wiederholt, 1)
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Offline geschrieben' } })
  assert.equal((await e).ok, true)
  assert.equal(aufrufe.length, 2)
  assert.equal(s.lesen(TOP), null)
})

await test('Abgelaufene Anmeldung: Eingabe bleibt gesichert, nach neuer Anmeldung gespeichert und gelöscht', async () => {
  const { d, sicherung: s } = aufbau([html(200, { redirected: true }), json({ success: true })])
  const e = d.senden(notiz('Vor dem Ablauf geschrieben'))
  await pause(2)
  assert.equal(d.stand().anmeldung, 'abgelaufen')
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Vor dem Ablauf geschrieben' } })
  d.jetztWiederholen() // Fokus zurück nach Anmeldung im anderen Tab
  assert.equal((await e).ok, true)
  assert.equal(s.lesen(TOP), null)
})

await test('Antwort 401: Eingabe bleibt gesichert und kommt nach der Frist in den localStorage', async () => {
  const { d, sicherung: s, speicher } = aufbau([json({}, 401)], { verzoegerungMs: 5 })
  void d.senden(notiz('Text'))
  await pause(30)
  assert.equal(d.stand().anmeldung, 'abgelaufen')
  assert.ok(s.lesen(TOP))
  assert.equal(speicher.sicherungen().length, 1)
})

await test('403 two_factor_setup_required: Hinweis mit Ziel, Eingabe bleibt gesichert', async () => {
  const { d, sicherung: s } = aufbau([json({ error: 'two_factor_setup_required', redirect: '/accounts/2fa/' }, 403)])
  void d.senden(position({ position: 'against' }))
  await pause(2)
  const stand = d.stand()
  assert.equal(stand.anmeldung, 'zweiter_faktor')
  assert.equal(stand.anmeldungZiel, '/accounts/2fa/')
  assert.deepEqual(werte(s.lesen(TOP)), { position: { position: 'against' } })
})

await test('Endgültiger Fehler (400): Eingabe bleibt gesichert', async () => {
  const { d, sicherung: s } = aufbau([json({ error: 'Ungültige Position' }, 400)])
  const e = await d.senden(position({ position: '?' }))
  assert.equal(e.ok, false)
  assert.ok(s.lesen(TOP))
})

await test('Einmal-Aufträge (Anlegen, Löschen) werden nicht gesichert', async () => {
  const { d, sicherung: s, speicher } = aufbau([json({}, 502)])
  await d.senden({ url: '/notes/', body: { content: 'Beitrag' }, sicherung: { top: TOP, bereich: 'x' } })
  assert.equal(s.lesen(TOP), null)
  s.festschreiben()
  assert.deepEqual(speicher.sicherungen(), [])
})

// ---------- Grundlage der Eingabe und Konflikte ----------

await test('Grundlage (vorher): Stand beim Laden, aus Echtzeit, unbekannt bleibt leer', async () => {
  const s = sicherung()
  s.serverstandSetzen(POSITION, { reasoning: 'Stand beim Laden', position: 'for' })
  s.merken(POSITION, { reasoning: 'Eigene Fassung' })
  assert.equal(s.lesen(TOP).bereiche.position.reasoning.vorher, 'Stand beim Laden')
  // Echtzeit: Kollegin speichert, danach getippt
  s.serverstandSetzen(POSITION, { reasoning: 'Fassung der Kollegin' })
  s.merken(POSITION, { reasoning: 'Eigene Fassung 2' })
  assert.equal(s.lesen(TOP).bereiche.position.reasoning.vorher, 'Fassung der Kollegin')
  // Unbekanntes Feld: ohne Grundlage
  s.merken(POSITION, { outcome: 'Angenommen' })
  assert.equal('vorher' in s.lesen(TOP).bereiche.position.outcome, false)
})

await test('vergleichen: schon gespeichert ist erledigt, sonst angeboten, anderweitig geändert ist ein Konflikt', async () => {
  const zeit = 5000
  const bereich = {
    position: { wert: 'for', vorher: 'open', zeit },
    reasoning: { wert: 'Offline geschrieben', vorher: '', zeit },
    outcome: { wert: 'Angenommen', vorher: '', zeit },
    is_final: { wert: true, zeit },
  }
  const server = { position: 'for', reasoning: 'Später von der Kollegin gespeichert', outcome: '', is_final: false }
  const { erledigt, angebot } = vergleichen(bereich, (feld) => server[feld])
  // Schon auf dem Server (z. B. beim Verlassen per keepalive angekommen)
  assert.deepEqual(erledigt, { position: 'for' })
  // Server unverändert seit der Eingabe: angeboten, kein Konflikt
  assert.deepEqual(angebot.outcome, { wert: 'Angenommen', server: '', konflikt: false, zeit })
  // Server inzwischen anderweitig geändert: Konflikt (Übernehmen würde den neueren Stand ersetzen)
  assert.equal(angebot.reasoning.konflikt, true)
  assert.equal(angebot.reasoning.server, 'Später von der Kollegin gespeichert')
  // Grundlage unbekannt: vorsichtshalber Konflikt
  assert.equal(angebot.is_final.konflikt, true)
  // Feld passt nicht mehr (Server liefert undefined): erledigt
  assert.deepEqual(vergleichen({ content: { wert: 'x', zeit } }, () => undefined).erledigt, { content: 'x' })
})

await test('uebernehmbar: seit dem Angebot auf der Seite geändert, wird nicht übernommen, sondern als Konflikt zurückgegeben', async () => {
  const angebot = {
    reasoning: { wert: 'Alte Eingabe', server: '', konflikt: false, zeit: 1 },
    outcome: { wert: 'Angenommen', server: '', konflikt: false, zeit: 1 },
    position: { wert: 'for', server: 'open', konflikt: true, zeit: 1 },
  }
  // Begründung inzwischen hier neu getippt bzw. per Echtzeit geändert; Ergebnis unverändert; Position unverändert
  const seite = { reasoning: 'Neue, gespeicherte Eingabe', outcome: '', position: 'open' }
  const { setzen, zurueck } = uebernehmbar(angebot, (feld) => seite[feld])
  assert.deepEqual(setzen, { outcome: 'Angenommen', position: 'for' })
  assert.deepEqual(zurueck, {
    reasoning: { wert: 'Alte Eingabe', server: 'Neue, gespeicherte Eingabe', konflikt: true, zeit: 1 },
  })
  // Auf der Seite steht schon die angebotene Eingabe: nichts zu tun
  assert.deepEqual(uebernehmbar({ outcome: angebot.outcome }, () => 'Angenommen'), { setzen: {}, zurueck: {} })
})

// ---------- Höchstens 24 Stunden, je Feld ----------

await test('Höchstens 24 Stunden je Feld: eine tägliche Notiz hält eine alte Begründung nicht am Leben', async () => {
  let uhr = 0
  const speicher = new Speicher()
  const s = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher, jetzt: () => uhr })
  s.merken(POSITION, { reasoning: 'Am ersten Tag abgelehnt' })
  s.festschreiben()
  uhr = HOECHSTDAUER_MS - 1000
  s.merken(NOTIZ, { content: 'Notiz am zweiten Tag' })
  assert.deepEqual(werte(s.lesen(TOP)), {
    position: { reasoning: 'Am ersten Tag abgelehnt' },
    notiz: { content: 'Notiz am zweiten Tag' },
  })
  uhr = HOECHSTDAUER_MS + 1
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Notiz am zweiten Tag' } })
  assert.doesNotMatch(speicher.getItem(speicher.sicherungen()[0]), /abgelehnt/, 'abgelaufenes Feld gelöscht')
  // Je Feld der eigene Zeitpunkt (für den Hinweis „vom …“)
  assert.equal(s.lesen(TOP).bereiche.notiz.content.zeit, HOECHSTDAUER_MS - 1000)
  // Eine neue Eingabe im selben Feld zählt neu
  s.merken(NOTIZ, { content: 'Notiz am dritten Tag' })
  uhr = HOECHSTDAUER_MS * 2
  assert.ok(s.lesen(TOP))
  uhr = HOECHSTDAUER_MS * 3
  assert.equal(s.lesen(TOP), null)
  assert.deepEqual(speicher.sicherungen(), [])
})

await test('Aufräumen beim Seitenaufruf: abgelaufene Felder und unlesbare Einträge aller Konten', async () => {
  const speicher = new Speicher()
  const alt = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher, jetzt: () => 0 })
  alt.merken(NOTIZ, { content: 'alt' })
  alt.festschreiben()
  let uhr = 0
  const gemischt = new Eingabensicherung(ANDERES_KONTO, ORG, SITZUNG, { speicher, jetzt: () => uhr })
  gemischt.merken(POSITION, { reasoning: 'alt' })
  uhr = HOECHSTDAUER_MS
  gemischt.merken(NOTIZ, { content: 'frisch' })
  gemischt.festschreiben()
  speicher.setItem(`${PRAEFIX}kaputt`, '{nicht json')
  // Format vor diesem Stand (ohne Zeit je Feld): unlesbar
  speicher.setItem(`${PRAEFIX}${KONTO}.${ORG}.${SITZUNG}.${TOP2}`, JSON.stringify({ zeit: 1, bereiche: { n: { c: 'x' } } }))
  speicher.setItem('mandari.vorbereitung.zoom', '4')
  aufraeumen(speicher, HOECHSTDAUER_MS + 10)
  assert.deepEqual(speicher.sicherungen(), [`${PRAEFIX}${ANDERES_KONTO}.${ORG}.${SITZUNG}.${TOP}`])
  assert.deepEqual(Object.keys(JSON.parse(speicher.getItem(speicher.sicherungen()[0])).bereiche), ['notiz'])
  assert.equal(speicher.getItem('mandari.vorbereitung.zoom'), '4')
})

// ---------- Konten, Abmelden ----------

await test('Je Konto getrennt: ein anderes Konto sieht die Eingaben nicht', async () => {
  const speicher = new Speicher()
  const eigen = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher })
  eigen.merken(NOTIZ, { content: 'privat' })
  eigen.festschreiben()
  const anderes = new Eingabensicherung(ANDERES_KONTO, ORG, SITZUNG, { speicher })
  assert.deepEqual(anderes.alle(), [])
  assert.equal(anderes.lesen(TOP), null)
  // Auch eine andere Sitzung desselben Kontos ist getrennt
  assert.deepEqual(new Eingabensicherung(KONTO, ORG, TOP2, { speicher }).alle(), [])
})

await test('Angemeldete Seite: Sicherungen anderer Konten werden gelöscht', async () => {
  const speicher = new Speicher()
  for (const konto of [KONTO, ANDERES_KONTO]) {
    const s = new Eingabensicherung(konto, ORG, SITZUNG, { speicher })
    s.merken(NOTIZ, { content: konto })
    s.festschreiben()
  }
  speicher.setItem('darkMode', 'true')
  speicher.setItem(ABGEMELDET, '123')
  aufraeumen(speicher, Date.now(), ANDERES_KONTO)
  assert.deepEqual(speicher.sicherungen(), [`${PRAEFIX}${ANDERES_KONTO}.${ORG}.${SITZUNG}.${TOP}`])
  assert.equal(speicher.getItem('darkMode'), 'true')
  assert.equal(speicher.getItem(ABGEMELDET), null)
  // Ohne gültige Kennung (nicht angemeldet): nur Abgelaufenes
  aufraeumen(speicher, Date.now(), '')
  assert.equal(speicher.sicherungen().length, 1)
})

await test('Abmelden löscht die Sicherungen aller Konten, andere Einstellungen bleiben', async () => {
  const speicher = new Speicher()
  for (const konto of [KONTO, ANDERES_KONTO]) {
    const s = new Eingabensicherung(konto, ORG, SITZUNG, { speicher })
    s.merken(NOTIZ, { content: konto })
    s.festschreiben()
  }
  speicher.setItem('darkMode', 'true')
  alleLoeschen(speicher, 4711)
  assert.deepEqual(speicher.sicherungen(), [])
  assert.equal(speicher.getItem('darkMode'), 'true')
  assert.equal(speicher.getItem(ABGEMELDET), '4711', 'Markierung für noch offene Seiten')
})

await test('Abmelden in einem anderen Tab: Die offene Seite sichert nichts mehr', async () => {
  const speicher = new Speicher()
  const fenster = new EventTarget()
  const s = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher, verzoegerungMs: 100000 })
  beiAbmeldungSperren(s, fenster)
  s.merken(NOTIZ, { content: 'Noch nicht bestätigt' })
  // Andere Schlüssel und das Entfernen der Markierung zählen nicht
  fenster.dispatchEvent(Object.assign(new Event('storage'), { key: 'darkMode', newValue: 'true' }))
  fenster.dispatchEvent(Object.assign(new Event('storage'), { key: ABGEMELDET, newValue: null }))
  assert.equal(s.aktiv, true)
  fenster.dispatchEvent(Object.assign(new Event('storage'), { key: ABGEMELDET, newValue: '4711' }))
  assert.equal(s.aktiv, false)
  s.festschreiben() // pagehide
  s.merken(NOTIZ, { content: 'Nach dem Abmelden getippt' })
  s.festschreiben()
  assert.deepEqual(speicher.sicherungen(), [])
  assert.equal(speicher.geschrieben, 0)
})

// ---------- Vergessen, Grenzfälle ----------

await test('Vergessen nur bei gleichem Wert; verwerfen löscht einen ganzen Bereich', async () => {
  const s = sicherung()
  s.merken({ top: TOP, bereich: 'rede' }, { content: '<p>Rede</p>', estimated_duration: 90 })
  s.merken(NOTIZ, { content: 'Notiz' })
  s.vergessen({ top: TOP, bereich: 'rede' }, { content: '<p>anders</p>', estimated_duration: 90 })
  assert.deepEqual(werte(s.lesen(TOP)).rede, { content: '<p>Rede</p>' })
  // Geerbte Eigenschaften sind nie gesicherte Felder
  s.vergessen({ top: TOP, bereich: 'rede' }, { toString: 'x' })
  s.gespeichert({ top: TOP, bereich: 'rede' }, { constructor: 'x' })
  s.verwerfen({ top: TOP, bereich: 'rede' })
  assert.deepEqual(werte(s.lesen(TOP)), { notiz: { content: 'Notiz' } })
  s.entfernen(TOP)
  assert.equal(s.lesen(TOP), null)
})

await test('Ohne nutzbaren localStorage (voll, gesperrt): Speichern läuft ohne Sicherung weiter', async () => {
  const voll = new Speicher()
  voll.setItem = () => {
    throw new DOMException('voll', 'QuotaExceededError')
  }
  const { d, sicherung: s } = aufbau([json({}, 502), json({ success: true })], { speicher: voll })
  const e = d.senden(notiz('Text'))
  s.festschreiben() // wirft nicht, auch nicht beim Aufräumen
  assert.equal((await e).ok, true)
  const gesperrt = new Eingabensicherung(KONTO, ORG, SITZUNG, { speicher: null })
  gesperrt.merken(NOTIZ, { content: 'x' })
  assert.deepEqual(gesperrt.alle(), [])
})

await test('Nur einfache Werte werden gesichert', async () => {
  const s = sicherung()
  s.merken({ top: TOP, bereich: 'rede' }, { linked_document: null, liste: [1], is_shared: true })
  assert.deepEqual(werte(s.lesen(TOP)), { rede: { is_shared: true } })
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
// Offene Wiederholungs-Timer (Anmeldeproblem-Tests) würden den Prozess sonst am Leben halten
process.exit(failed > 0 ? 1 : 0)
