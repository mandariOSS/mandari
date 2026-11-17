/**
 * Tests für die Meldungen unten rechts (`frontend/js/alpine/toast.ts`) und die Hinweise auf neue Benachrichtigungen
 * (`static/js/notifications.js`): Jede Meldung steht einmal da und lässt sich schließen.
 *
 * - Dieselbe Meldung noch einmal ergibt keine zweite, höchstens vier stehen gleichzeitig.
 * - Unter dem Zeiger oder mit Tastaturfokus bleibt eine Meldung stehen, danach verschwindet sie.
 * - Eine aus dem Verlauf zurückgeholte Seite zeigt ihre Meldungen nicht noch einmal.
 * - Eine Benachrichtigung wird in einem Browser einmal eingeblendet, nicht bei jedem Seitenaufruf.
 *
 * Ausführen mit: npm run test:meldungen
 * (baut toast.ts via esbuild nach CJS, notifications.js läuft in einem eigenen Kontext)
 */

import assert from 'node:assert/strict'
import { execSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const ziel = join(here, 'build', 'toast.cjs')
execSync(`npx esbuild frontend/js/alpine/toast.ts --bundle --format=cjs --platform=node --outfile="${ziel}"`, {
  cwd: projectRoot,
  stdio: 'inherit',
})

// ---------- Umgebung: Uhr von Hand, Fenster und Dokument als Attrappen ----------

let jetzt = 0
let naechsteFrist = 1
const fristen = new Map()
const uhr = {
  setTimeout(fn, dauer = 0) {
    const id = naechsteFrist++
    fristen.set(id, { fn, wann: jetzt + dauer })
    return id
  },
  clearTimeout(id) {
    fristen.delete(id)
  },
  /** Zeit vorstellen und fällige Fristen in ihrer Reihenfolge auslösen */
  vor(ms) {
    const ende = jetzt + ms
    for (;;) {
      const faellig = [...fristen].filter(([, f]) => f.wann <= ende).sort((a, b) => a[1].wann - b[1].wann)[0]
      if (!faellig) break
      fristen.delete(faellig[0])
      jetzt = faellig[1].wann
      faellig[1].fn()
    }
    jetzt = ende
  },
}

const HERKUNFT = 'https://mandari.example'
let zeigerKannSchweben = true
let serverMeldungen = null
let seitenMarke

globalThis.window = globalThis
globalThis.setTimeout = uhr.setTimeout
globalThis.clearTimeout = uhr.clearTimeout
globalThis.innerHeight = 800
globalThis.location = { origin: HERKUNFT }
globalThis.matchMedia = () => ({ matches: zeigerKannSchweben })
globalThis.addEventListener = () => {}
globalThis.document = {
  body: { addEventListener() {} },
  querySelectorAll: () => [],
  getElementById: (id) => (id === 'django-messages' ? serverMeldungen : null),
}

function speicher() {
  const werte = new Map()
  return {
    werte,
    getItem: (schluessel) => (werte.has(schluessel) ? werte.get(schluessel) : null),
    setItem: (schluessel, wert) => void werte.set(schluessel, String(wert)),
  }
}
globalThis.sessionStorage = speicher()

const require = createRequire(import.meta.url)
const { toastManager, gleicheMeldung, sichererLink, schonGezeigt, MAX_SICHTBAR } = require(ziel)

/** Komponente wie von Alpine eingesetzt; `benutzt` steht für Zeiger darauf oder Fokus darin */
function anzeige() {
  const k = toastManager()
  k.benutzt = { zeiger: false, fokus: false }
  k.$root = {
    style: {},
    dataset: seitenMarke === undefined ? {} : { marke: seitenMarke },
    matches: (auswahl) => (auswahl === ':hover' ? k.benutzt.zeiger : auswahl === ':focus-within' ? k.benutzt.fokus : false),
  }
  return k
}

const sichtbar = (k) => k.toasts.filter((t) => t.visible).map((t) => t.message)

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

// ---------- Anzeige ----------

test('Eine Meldung verschwindet nach fünf Sekunden von selbst', () => {
  const k = anzeige()
  k.addToast('Gespeichert.', 'success')
  uhr.vor(4900)
  assert.deepEqual(sichtbar(k), ['Gespeichert.'])
  uhr.vor(200)
  assert.deepEqual(sichtbar(k), [])
  uhr.vor(400)
  assert.equal(k.toasts.length, 0)
})

test('Schließen entfernt die Meldung sofort, die übrigen bleiben', () => {
  const k = anzeige()
  k.addToast('Eins', 'info')
  k.addToast('Zwei', 'info')
  k.removeToast(k.toasts[0].id)
  assert.deepEqual(sichtbar(k), ['Zwei'])
  uhr.vor(400)
  assert.deepEqual(
    k.toasts.map((t) => t.message),
    ['Zwei'],
  )
  uhr.vor(6000)
})

test('Dieselbe Meldung noch einmal ergibt keine zweite, sie bleibt nur länger stehen', () => {
  const k = anzeige()
  k.addToast('Keine Verbindung zum Server.', 'error')
  uhr.vor(4000)
  k.addToast('Keine Verbindung zum Server.', 'error')
  assert.equal(k.toasts.length, 1)
  uhr.vor(4000)
  assert.deepEqual(sichtbar(k), ['Keine Verbindung zum Server.'], 'Frist beginnt mit der Wiederholung neu')
  uhr.vor(1100)
  assert.deepEqual(sichtbar(k), [])
  uhr.vor(400)
})

test('Gleicher Text mit anderer Art oder anderer Überschrift ist eine eigene Meldung', () => {
  const k = anzeige()
  k.addToast('Aufgabe geändert', 'info')
  k.addToast('Aufgabe geändert', 'error')
  k.addToast('Aufgabe geändert', 'info', { titel: 'Haushalt 2027' })
  assert.equal(k.toasts.length, 3)
  uhr.vor(6000)
})

test('Eine geschlossene Meldung darf wiederkommen, wenn sie erneut ausgelöst wird', () => {
  const k = anzeige()
  k.addToast('Link kopiert', 'success')
  k.removeToast(k.toasts[0].id)
  k.addToast('Link kopiert', 'success')
  assert.deepEqual(sichtbar(k), ['Link kopiert'])
  uhr.vor(6000)
})

test('Höchstens vier Meldungen gleichzeitig, die älteste weicht', () => {
  const k = anzeige()
  for (let i = 1; i <= MAX_SICHTBAR + 2; i++) k.addToast(`Meldung ${i}`, 'info')
  assert.deepEqual(sichtbar(k), ['Meldung 3', 'Meldung 4', 'Meldung 5', 'Meldung 6'])
  uhr.vor(6000)
})

test('Unter dem Zeiger bleibt die Meldung stehen und verschwindet danach', () => {
  const k = anzeige()
  k.addToast('Beschluss erfasst', 'success')
  k.benutzt.zeiger = true
  uhr.vor(20000)
  assert.deepEqual(sichtbar(k), ['Beschluss erfasst'])
  k.benutzt.zeiger = false
  uhr.vor(1600)
  assert.deepEqual(sichtbar(k), [])
  uhr.vor(400)
})

test('Mit Tastaturfokus in der Meldung bleibt sie stehen', () => {
  const k = anzeige()
  k.addToast('Neue Benachrichtigung', 'info', { link: '/work/fraktion/notifications/' })
  k.benutzt.fokus = true
  uhr.vor(20000)
  assert.deepEqual(sichtbar(k), ['Neue Benachrichtigung'])
  k.benutzt.fokus = false
  uhr.vor(1600)
  assert.deepEqual(sichtbar(k), [])
  uhr.vor(400)
})

test('Am Touch-Gerät hält ein hängengebliebenes „hover“ die Meldung nicht fest', () => {
  const k = anzeige()
  zeigerKannSchweben = false
  k.addToast('Gespeichert.', 'success')
  k.benutzt.zeiger = true
  uhr.vor(5100)
  assert.deepEqual(sichtbar(k), [])
  zeigerKannSchweben = true
  uhr.vor(400)
})

test('Schließen unter dem Zeiger hält spätere Meldungen nicht dauerhaft fest', () => {
  const k = anzeige()
  k.addToast('Eins', 'info')
  k.benutzt.zeiger = true
  k.removeToast(k.toasts[0].id)
  // Die Meldung ist weg, der Zeiger steht nicht mehr darauf
  k.benutzt.zeiger = false
  k.addToast('Zwei', 'info')
  uhr.vor(5100)
  assert.deepEqual(sichtbar(k), [])
  uhr.vor(400)
})

test('Überschrift und Ziel kommen aus dem Ereignis, fremde Ziele nicht', () => {
  const k = anzeige()
  k.addToast('Bitte bis Freitag prüfen', 'info', { titel: 'Neue Aufgabe', link: '/work/fraktion/tasks/7/' })
  k.addToast('Fremd', 'info', { link: 'https://boese.example/x' })
  k.addToast('Skript', 'info', { link: 'javascript:alert(1)' })
  // htmx hängt bei Server-Meldungen das auslösende Element an; Überschrift und Ziel fehlen dann
  k.addToast('Vom Server', 'success', { message: 'Vom Server', type: 'success', elt: {} })
  assert.deepEqual(
    k.toasts.map((t) => [t.titel, t.link]),
    [
      ['Neue Aufgabe', '/work/fraktion/tasks/7/'],
      ['', ''],
      ['', ''],
      ['', ''],
    ],
  )
  uhr.vor(6000)
})

test('sichererLink: nur Adressen dieser Anwendung, als Pfad', () => {
  assert.equal(sichererLink('/work/a/b/?x=1#y', HERKUNFT), '/work/a/b/?x=1#y')
  assert.equal(sichererLink(`${HERKUNFT}/work/a/`, HERKUNFT), '/work/a/')
  assert.equal(sichererLink('//boese.example/x', HERKUNFT), '')
  assert.equal(sichererLink('/\\boese.example/x', HERKUNFT), '')
  assert.equal(sichererLink('https://boese.example/', HERKUNFT), '')
  assert.equal(sichererLink('javascript:alert(1)', HERKUNFT), '')
  assert.equal(sichererLink('', HERKUNFT), '')
  assert.equal(sichererLink(undefined, HERKUNFT), '')
  assert.equal(sichererLink({ href: '/x' }, HERKUNFT), '')
})

test('gleicheMeldung findet nur sichtbare Meldungen', () => {
  const liste = [{ id: 1, message: 'A', type: 'info', visible: false, titel: '', link: '' }]
  assert.equal(gleicheMeldung(liste, 'A', 'info', '', ''), undefined)
  liste[0].visible = true
  assert.equal(gleicheMeldung(liste, 'A', 'info', '', '')?.id, 1)
})

test('Gleicher Hinweis mit anderem Ziel ist eine eigene Meldung: Kein Ziel geht verloren', () => {
  const k = anzeige()
  k.addToast('Bitte prüfen', 'info', { titel: 'Neue Aufgabe', link: '/work/fraktion/tasks/7/' })
  k.addToast('Bitte prüfen', 'info', { titel: 'Neue Aufgabe', link: '/work/fraktion/tasks/8/' })
  k.addToast('Bitte prüfen', 'info', { titel: 'Neue Aufgabe', link: '/work/fraktion/tasks/8/' })
  assert.deepEqual(
    k.toasts.map((t) => t.link),
    ['/work/fraktion/tasks/7/', '/work/fraktion/tasks/8/'],
  )
  uhr.vor(6000)
})

// ---------- Meldungen des Servers ----------

function seiteMit(meldungen, marke) {
  serverMeldungen = { textContent: JSON.stringify(meldungen) }
  seitenMarke = marke
}

test('Meldungen des Servers erscheinen beim Laden der Seite', () => {
  globalThis.sessionStorage = speicher()
  seiteMit([{ message: 'Einstellung gespeichert', tags: 'success' }], '1794912345.123456')
  const k = anzeige()
  k.init()
  assert.deepEqual(sichtbar(k), ['Einstellung gespeichert'])
  assert.equal(k.toasts[0].type, 'success')
  uhr.vor(6000)
})

test('Dieselbe Seite aus dem Verlauf zeigt ihre Meldungen nicht noch einmal', () => {
  globalThis.sessionStorage = speicher()
  seiteMit([{ message: 'Aufgabe gelöscht', tags: 'success' }], '1794912345.000001')
  const zuerst = anzeige()
  zuerst.init()
  assert.deepEqual(sichtbar(zuerst), ['Aufgabe gelöscht'])
  uhr.vor(6000)
  const zurueck = anzeige()
  zurueck.init()
  assert.deepEqual(sichtbar(zurueck), [])
  // Eine neue Antwort des Servers hat eine andere Kennung
  seiteMit([{ message: 'Aufgabe gelöscht', tags: 'success' }], '1794912399.000002')
  const neu = anzeige()
  neu.init()
  assert.deepEqual(sichtbar(neu), ['Aufgabe gelöscht'])
  uhr.vor(6000)
})

test('Ohne Kennung oder ohne Speicher erscheinen die Meldungen wie bisher', () => {
  globalThis.sessionStorage = speicher()
  seiteMit([{ message: 'Ohne Kennung', tags: 'info' }])
  for (let i = 0; i < 2; i++) {
    const k = anzeige()
    k.init()
    assert.deepEqual(sichtbar(k), ['Ohne Kennung'])
    uhr.vor(6000)
  }
  assert.equal(schonGezeigt('1.5', null), false)
  const gesperrt = {
    getItem() {
      throw new Error('gesperrt')
    },
    setItem() {
      throw new Error('gesperrt')
    },
  }
  assert.equal(schonGezeigt('1.5', gesperrt), false)
})

test('Seite ohne Meldungen merkt sich nichts', () => {
  const s = speicher()
  globalThis.sessionStorage = s
  seiteMit([], undefined)
  const k = anzeige()
  k.init()
  assert.deepEqual(sichtbar(k), [])
  assert.equal(s.werte.size, 0)
})

test('schonGezeigt behält nur die letzten Seitenstände und verträgt fremde Inhalte', () => {
  const s = speicher()
  for (let i = 0; i < 30; i++) assert.equal(schonGezeigt(`m${i}`, s), false)
  assert.equal(schonGezeigt('m29', s), true)
  assert.equal(schonGezeigt('m0', s), false, 'alte Stände fallen heraus')
  s.setItem('mandari:meldungen-gezeigt', '{"kein": "Feld"}')
  assert.equal(schonGezeigt('m5', s), false)
  s.setItem('mandari:meldungen-gezeigt', 'kein JSON')
  assert.equal(schonGezeigt('m5', s), false)
})

// ---------- Hinweise auf neue Benachrichtigungen (static/js/notifications.js) ----------

const NOTIFICATIONS = readFileSync(join(projectRoot, 'static', 'js', 'notifications.js'), 'utf8')

/** Eine Seite in Work mit eigenem Skriptlauf; `browser` (Speicher) überdauert Seitenwechsel */
function seite(browser, antworten) {
  const gezeigt = []
  const abrufe = []
  const fenster = {
    location: { pathname: '/work/fraktion/tasks/' },
    localStorage: browser,
    dispatchEvent(ereignis) {
      // Als eigene Objekte dieses Kontexts, damit der Vergleich nicht an fremden Prototypen scheitert
      if (ereignis.type === 'show-toast') gezeigt.push(JSON.parse(JSON.stringify(ereignis.detail)))
    },
  }
  const kontext = {
    window: fenster,
    document: {
      readyState: 'complete',
      title: 'Aufgaben | Fraktion',
      visibilityState: 'visible',
      querySelector: () => ({ dataset: { orgSlug: 'fraktion', konto: '17' } }),
      addEventListener() {},
    },
    CustomEvent: class {
      constructor(type, init) {
        this.type = type
        this.detail = init?.detail
      }
    },
    console: { ...console, error() {} },
    setInterval: () => 1,
    // Die Frist eines Abrufs läuft über die Uhr des Tests
    setTimeout: uhr.setTimeout,
    clearTimeout: uhr.clearTimeout,
    AbortController,
    encodeURIComponent,
    fetch: async (adresse, angaben) => {
      abrufe.push(adresse)
      if (antworten[0] === HAENGT) {
        antworten.shift()
        // Eine Antwort, die nie kommt: endet nur durch den Abbruch
        await new Promise((_, abgelehnt) => {
          angaben.signal.addEventListener('abort', () => abgelehnt(new Error('abgebrochen')))
        })
      }
      // Ein Eintrag darf ein Versprechen sein: So lässt sich eine Antwort im Test verzögern
      const antwort = await (antworten.shift() ?? { notifications: [], count: 0 })
      return { ok: true, json: async () => antwort }
    },
  }
  vm.runInNewContext(NOTIFICATIONS, kontext)
  return { gezeigt, abrufe, fenster, dokument: kontext.document, modul: fenster.MandariNotifications }
}

const warten = () => new Promise((fertig) => setImmediate(fertig))
/** Platzhalter in der Liste der Antworten: Dieser Abruf bekommt keine Antwort */
const HAENGT = Symbol('hängt')
const meldung = (nr, zeit) => ({
  id: `n${nr}`,
  title: `Titel ${nr}`,
  message: `Text ${nr}`,
  link: `/work/fraktion/tasks/${nr}/`,
  created_at: zeit,
})
const T1 = '2026-11-17T09:00:00.100000+00:00'
const T2 = '2026-11-17T09:05:00.200000+00:00'
const T3 = '2026-11-17T09:06:00.300000+00:00'
const SCHLUESSEL = 'mandari:hinweise-stand:17:fraktion'

async function asyncTest(name, fn) {
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

await asyncTest('Erster Besuch in einem Browser: nichts nachholen, Stand merken', async () => {
  const browser = speicher()
  const s = seite(browser, [{ notifications: [meldung(1, T1)], count: 1 }])
  await warten()
  assert.deepEqual(s.gezeigt, [])
  assert.equal(browser.getItem(SCHLUESSEL), T1)
  assert.deepEqual(s.abrufe, ['/work/fraktion/notifications/latest/'])
  assert.equal(s.dokument.title, '(1) Aufgaben | Fraktion')
})

await asyncTest('Ungelesene Benachrichtigung erscheint nicht bei jedem Seitenaufruf erneut', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  // Der Server liefert mit since nur Neueres: nichts, obwohl eine ungelesen ist (count 1)
  for (let aufruf = 0; aufruf < 3; aufruf++) {
    const s = seite(browser, [{ notifications: [], count: 1 }])
    await warten()
    assert.deepEqual(s.gezeigt, [])
    assert.deepEqual(s.abrufe, [`/work/fraktion/notifications/latest/?since=${encodeURIComponent(T1)}`])
    assert.equal(s.dokument.title, '(1) Aufgaben | Fraktion')
  }
  assert.equal(browser.getItem(SCHLUESSEL), T1)
})

await asyncTest('Neue Benachrichtigung: einmal einblenden, mit Titel und Ziel, danach nicht mehr', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  const s = seite(browser, [
    { notifications: [meldung(2, T2)], count: 2 },
    { notifications: [], count: 2 },
  ])
  await warten()
  assert.deepEqual(s.gezeigt, [
    { message: 'Text 2', titel: 'Titel 2', link: '/work/fraktion/tasks/2/', type: 'info' },
  ])
  assert.equal(browser.getItem(SCHLUESSEL), T2)
  await s.modul.poll()
  assert.equal(s.gezeigt.length, 1)
  assert.equal(s.abrufe[1], `/work/fraktion/notifications/latest/?since=${encodeURIComponent(T2)}`)
  // Nächste Seite, anderer Reiter: derselbe Stand im Browser
  const naechste = seite(browser, [{ notifications: [], count: 2 }])
  await warten()
  assert.deepEqual(naechste.gezeigt, [])
})

await asyncTest('Mehrere neue Benachrichtigungen: ein Hinweis auf die Übersicht', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  const s = seite(browser, [{ notifications: [meldung(3, T3), meldung(2, T2)], count: 3 }])
  await warten()
  assert.deepEqual(s.gezeigt, [
    { message: 'Sie haben 2 neue Benachrichtigungen.', titel: '', link: '/work/fraktion/notifications/', type: 'info' },
  ])
  assert.equal(browser.getItem(SCHLUESSEL), T3, 'Stand ist die neueste')
  const viele = s.modul.hinweis(
    [1, 2, 3, 4, 5].map((nr) => meldung(nr, T1)),
    '/work/fraktion/notifications/',
  )
  assert.equal(viele.message, 'Sie haben mehrere neue Benachrichtigungen.')
})

await asyncTest('Beim ersten Besuch gab es keine ungelesene: die nächste ist neu', async () => {
  const browser = speicher()
  const s = seite(browser, [
    { notifications: [], count: 0 },
    { notifications: [meldung(1, T1)], count: 1 },
  ])
  await warten()
  assert.equal(browser.getItem(SCHLUESSEL), '')
  assert.equal(s.dokument.title, 'Aufgaben | Fraktion')
  await s.modul.poll()
  assert.equal(s.gezeigt.length, 1)
  assert.equal(s.abrufe[1], '/work/fraktion/notifications/latest/', 'ohne Stand kein since')
  assert.equal(browser.getItem(SCHLUESSEL), T1)
})

await asyncTest('Browser ohne Speicher: je Seite einmal, kein Fehler', async () => {
  const gesperrt = {
    getItem() {
      throw new Error('gesperrt')
    },
    setItem() {
      throw new Error('gesperrt')
    },
  }
  const s = seite(gesperrt, [
    { notifications: [meldung(1, T1)], count: 1 },
    { notifications: [meldung(2, T2)], count: 2 },
    { notifications: [], count: 2 },
  ])
  await warten()
  assert.deepEqual(s.gezeigt, [])
  await s.modul.poll()
  assert.equal(s.gezeigt.length, 1)
  assert.equal(s.abrufe[1], `/work/fraktion/notifications/latest/?since=${encodeURIComponent(T1)}`)
  await s.modul.poll()
  assert.equal(s.gezeigt.length, 1)
})

await asyncTest('Speicher lässt sich lesen, aber nicht schreiben: trotzdem nur einmal', async () => {
  const werte = new Map([[SCHLUESSEL, T1]])
  const nurLesen = {
    getItem: (schluessel) => (werte.has(schluessel) ? werte.get(schluessel) : null),
    setItem() {
      throw new Error('voll')
    },
  }
  const s = seite(nurLesen, [
    { notifications: [meldung(2, T2)], count: 2 },
    { notifications: [], count: 2 },
    { notifications: [], count: 2 },
  ])
  await warten()
  assert.equal(s.gezeigt.length, 1)
  await s.modul.poll()
  await s.modul.poll()
  assert.equal(s.gezeigt.length, 1)
  // Der Stand dieser Seite ist weiter als der Speicher und gilt für die nächsten Abrufe
  assert.deepEqual(s.abrufe.slice(1), [
    `/work/fraktion/notifications/latest/?since=${encodeURIComponent(T2)}`,
    `/work/fraktion/notifications/latest/?since=${encodeURIComponent(T2)}`,
  ])
})

await asyncTest('Ein Abruf zur Zeit: Ein zweiter wartet nicht daneben', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  let antworten
  const spaet = new Promise((fertig) => {
    antworten = fertig
  })
  const s = seite(browser, [spaet])
  await warten()
  await s.modul.poll()
  await s.modul.poll()
  assert.equal(s.abrufe.length, 1, 'solange der erste Abruf läuft, geht kein zweiter raus')
  antworten({ notifications: [meldung(2, T2)], count: 2 })
  await warten()
  assert.equal(s.gezeigt.length, 1)
  await s.modul.poll()
  assert.equal(s.abrufe.length, 2)
  assert.equal(s.gezeigt.length, 1)
})

await asyncTest('Ein Abruf ohne Antwort endet nach der Frist und hält die nächsten nicht auf', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  const s = seite(browser, [HAENGT, { notifications: [meldung(2, T2)], count: 2 }])
  await warten()
  await s.modul.poll()
  assert.equal(s.abrufe.length, 1, 'der hängende Abruf sperrt zunächst')
  uhr.vor(8100)
  await warten()
  await s.modul.poll()
  assert.equal(s.abrufe.length, 2)
  assert.equal(s.gezeigt.length, 1)
})

await asyncTest('Verspätete Antwort eines anderen Reiters: nichts doppelt, der Stand geht nicht zurück', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  let antworten
  const spaet = new Promise((fertig) => {
    antworten = fertig
  })
  // Reiter A fragt mit dem Stand T1, seine Antwort (nur die zweite Benachrichtigung) lässt auf sich warten
  const a = seite(browser, [spaet])
  await warten()
  // Reiter B bekommt inzwischen beide neuen und merkt sich die neueste
  const b = seite(browser, [{ notifications: [meldung(3, T3), meldung(2, T2)], count: 3 }])
  await warten()
  assert.equal(b.gezeigt.length, 1)
  assert.equal(browser.getItem(SCHLUESSEL), T3)
  antworten({ notifications: [meldung(2, T2)], count: 3 })
  await warten()
  assert.deepEqual(a.gezeigt, [], 'die zweite Benachrichtigung hat Reiter B schon gezeigt')
  assert.equal(browser.getItem(SCHLUESSEL), T3, 'eine ältere Antwort setzt den Stand nicht zurück')
})

await asyncTest('auswerten: nur Späteres zeigen, Stand nie zurück, gleiche Millisekunde nach Schreibweise', async () => {
  const { modul } = seite(speicher(), [])
  await warten()
  const klar = (wert) => JSON.parse(JSON.stringify(wert))
  assert.deepEqual(klar(modul.auswerten([meldung(2, T2)], T3)), { zeigen: [], stand: T3 })
  assert.deepEqual(klar(modul.auswerten([meldung(3, T3), meldung(2, T2)], T2)), { zeigen: [meldung(3, T3)], stand: T3 })
  assert.deepEqual(klar(modul.auswerten([meldung(1, T1)], '')), { zeigen: [meldung(1, T1)], stand: T1 })
  assert.deepEqual(klar(modul.auswerten([], T1)), { zeigen: [], stand: T1 })
  assert.deepEqual(klar(modul.auswerten([meldung(1, T1)], undefined)), { zeigen: [], stand: T1 })
  assert.deepEqual(klar(modul.auswerten('kein Feld', T1)), { zeigen: [], stand: T1 })
  const frueher = '2026-11-17T09:00:00.100100+00:00'
  const spaeter = '2026-11-17T09:00:00.100900+00:00'
  assert.deepEqual(klar(modul.auswerten([meldung(5, spaeter)], frueher)).zeigen.length, 1)
  assert.deepEqual(klar(modul.auswerten([meldung(4, frueher)], spaeter)), { zeigen: [], stand: spaeter })
  // Unlesbarer Stand im Speicher (von Hand geändert): alles gilt als neu, danach stimmt der Stand wieder
  assert.deepEqual(klar(modul.auswerten([meldung(1, T1)], 'kaputt')), { zeigen: [meldung(1, T1)], stand: T1 })
})

await asyncTest('Benachrichtigung ohne Text: der Titel ist die Meldung; ohne Ziel führt sie zur Übersicht', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  const s = seite(browser, [
    { notifications: [{ id: 'n9', title: 'Sitzung beginnt', message: '', link: '', created_at: T2 }], count: 1 },
  ])
  await warten()
  assert.deepEqual(s.gezeigt, [
    { message: 'Sitzung beginnt', titel: '', link: '/work/fraktion/notifications/', type: 'info' },
  ])
})

await asyncTest('Zahl im Seitentitel folgt dem Stand, auch wenn sie sich ändert', async () => {
  const browser = speicher()
  browser.setItem(SCHLUESSEL, T1)
  const s = seite(browser, [
    { notifications: [], count: 3 },
    { notifications: [], count: 120 },
    { notifications: [], count: 0 },
  ])
  await warten()
  assert.equal(s.dokument.title, '(3) Aufgaben | Fraktion')
  await s.modul.poll()
  assert.equal(s.dokument.title, '(99+) Aufgaben | Fraktion')
  await s.modul.poll()
  assert.equal(s.dokument.title, 'Aufgaben | Fraktion')
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
process.exit(failed > 0 ? 1 : 0)
