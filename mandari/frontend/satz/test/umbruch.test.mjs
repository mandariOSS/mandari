/**
 * Tests für den Seitenumbruch der Satz-Kette (`frontend/satz/umbruch.ts`, Issue #1584).
 *
 * Ausführen mit: npm run test:satz
 * (baut umbruch.ts via esbuild nach CJS; die Blöcke sind gedachte Messwerte, kein Browser nötig)
 */

import assert from 'node:assert/strict'
import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'umbruch.cjs')

execSync(`npx esbuild frontend/satz/umbruch.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`, {
  cwd: projectRoot,
  stdio: 'inherit',
})

const require = createRequire(import.meta.url)
const { umbrechen } = require(outFile)

// Einfache Seite: 1000 px hoch, Satzspiegel 100–900 auf jeder Seite, 20 px Lücke zwischen den Seiten
const g = {
  breite: 800,
  hoehe: 1000,
  links: 80,
  rechts: 80,
  erste: { oben: 100, unten: 100 },
  folge: { oben: 100, unten: 100 },
  luecke: 20,
}

let pos = 1
/** Absatz ab `oben` mit `n` Zeilen zu je 20 px */
function absatz(oben, n, extra = {}) {
  const zeilen = Array.from({ length: n }, (_, i) => ({ oben: oben + i * 20, unten: oben + (i + 1) * 20, pos: pos + i }))
  const block = { art: 'text', oben, unten: oben + n * 20, zeilen, anker: pos, mitNaechstem: false, zusammen: false, ...extra }
  pos += n + 2
  return block
}
function kopf(oben, hoehe = 30) {
  const block = {
    art: 'kopf',
    oben,
    unten: oben + hoehe,
    zeilen: [{ oben, unten: oben + hoehe, pos }],
    anker: pos,
    mitNaechstem: true,
    zusammen: true,
  }
  pos += 3
  return block
}
function zeile(oben, hoehe) {
  const block = { art: 'zeile', oben, unten: oben + hoehe, zeilen: [], anker: pos, mitNaechstem: false, zusammen: true }
  pos += 5
  return block
}
function umbruch(oben) {
  const block = { art: 'umbruch', oben, unten: oben, zeilen: [], anker: pos, mitNaechstem: false, zusammen: true }
  pos += 1
  return block
}

const ergebnisse = []
function test(name, fn) {
  try {
    fn()
    ergebnisse.push(['ok', name])
  } catch (err) {
    ergebnisse.push(['FEHLER', name, err])
  }
}

test('Alles passt auf eine Seite: keine Lücke', () => {
  const r = umbrechen([absatz(100, 10), absatz(310, 10)], g)
  assert.equal(r.seiten, 1)
  assert.deepEqual(r.luecken, [])
})

test('Absatz wird zwischen Zeilen geteilt; die Folgezeile beginnt oben auf Seite 2', () => {
  // 30 Zeilen ab 400: Zeilen bis 900 passen (25 Zeilen), 5 wandern
  const a = absatz(400, 30)
  const r = umbrechen([a], g)
  assert.equal(r.seiten, 2)
  assert.equal(r.luecken.length, 1)
  const l = r.luecken[0]
  assert.equal(l.art, 'zeile')
  assert.equal(l.pos, a.zeilen[25].pos)
  // Zeile 25 stand bei 900, soll bei 1020 + 100 = 1120 stehen
  assert.equal(l.px, 220)
})

test('Keine einzelne Zeile am Seitenende: Absatz wandert ganz', () => {
  // Nur eine Zeile hätte noch Platz (880–900)
  const a = absatz(880, 6)
  const r = umbrechen([absatz(100, 39), a], g)
  assert.equal(r.luecken.length, 1)
  assert.equal(r.luecken[0].art, 'block')
  assert.equal(r.luecken[0].pos, a.anker)
  assert.equal(r.luecken[0].px, 1120 - 880)
})

test('Keine einzelne Zeile am Seitenanfang: eine Zeile mehr mitnehmen', () => {
  // 21 Zeilen ab 500: 20 passen, nur 1 bliebe für Seite 2 → zwei wandern (Zeile 19)
  const a = absatz(500, 21)
  const r = umbrechen([a], g)
  assert.equal(r.luecken.length, 1)
  assert.equal(r.luecken[0].pos, a.zeilen[19].pos)
})

test('Überschrift bleibt beim folgenden Absatz', () => {
  const k = kopf(850)
  const a = absatz(880, 5)
  const r = umbrechen([absatz(100, 37), k, a], g)
  // Die Überschrift passt (850–880), aber vom Absatz passt keine zweite Zeile mehr: beide auf Seite 2
  assert.equal(r.luecken[0].pos, k.anker)
  assert.equal(r.seiten, 2)
})

test('Tabellenzeile wird nicht geteilt und bekommt eine Tabellenlücke', () => {
  const z = zeile(880, 60)
  const r = umbrechen([absatz(100, 30), zeile(700, 60), z], g)
  assert.equal(r.luecken.length, 1)
  assert.equal(r.luecken[0].art, 'tabelle')
  assert.equal(r.luecken[0].pos, z.anker)
})

test('Manueller Seitenumbruch schiebt den nächsten Block auf die nächste Seite', () => {
  const b = absatz(330, 2)
  const r = umbrechen([absatz(100, 10), umbruch(310), b], g)
  assert.equal(r.seiten, 2)
  assert.equal(r.luecken[0].pos, b.anker)
  assert.equal(r.luecken[0].px, 1120 - 330)
})

test('Umbruch am Ende beginnt eine leere Seite', () => {
  const r = umbrechen([absatz(100, 3), umbruch(160)], g)
  assert.equal(r.seiten, 2)
})

test('Langer Text über mehrere Seiten: jede Lücke beginnt am Satzspiegel der Folgeseite', () => {
  const a = absatz(100, 100) // 2000 px Text, 40 Zeilen je Seite
  const r = umbrechen([a], g)
  assert.equal(r.seiten, 3)
  assert.deepEqual(
    r.luecken.map((l) => l.pos),
    [a.zeilen[40].pos, a.zeilen[80].pos],
  )
  // Jede Lücke überbrückt Rand unten, Lücke und Rand oben: 100 + 20 + 100
  for (const l of r.luecken) assert.equal(l.px, 220)
})

test('Block höher als eine Seite ragt hinaus und wird gezählt', () => {
  const bild = { art: 'atom', oben: 300, unten: 1300, zeilen: [], anker: 999, mitNaechstem: false, zusammen: true }
  const r = umbrechen([absatz(100, 5), bild], g)
  assert.equal(r.zuHoch, 1)
  assert.equal(r.luecken[0].pos, 999)
})

test('Block, der im Rand der Folgeseite beginnen würde, rückt an den Satzspiegel', () => {
  // Absatz endet genau am Satzspiegel (900), der nächste beginnt nach großem Abstand bei 950 (im unteren Rand)
  const b = absatz(950, 2)
  const r = umbrechen([absatz(100, 40), b], g)
  assert.equal(r.luecken[0].pos, b.anker)
  assert.equal(r.luecken[0].px, 1120 - 950)
})

test('Zwei Umbrüche hintereinander ergeben eine leere Seite', () => {
  const b = absatz(330, 2)
  const r = umbrechen([absatz(100, 10), umbruch(310), umbruch(310), b], g)
  assert.equal(r.seiten, 3)
  // Der Absatz steht oben auf Seite 3: 2 * 1020 + 100
  assert.equal(r.luecken.reduce((s, l) => s + l.px, 0), 2140 - 330)
})

test('Überschrift vor einem dreizeiligen Absatz, der nicht mehr ganz passt, wandert mit', () => {
  // Überschrift 830–860, Absatz 860–920 (drei Zeilen): nicht teilbar, passt nicht → beide auf Seite 2
  const k = kopf(830)
  const r = umbrechen([absatz(100, 36), k, absatz(860, 3)], g)
  assert.equal(r.luecken[0].pos, k.anker)
})

let fehler = 0
for (const [stand, name, err] of ergebnisse) {
  console.log(`${stand}  ${name}`)
  if (err) {
    fehler += 1
    console.log(err)
  }
}
if (fehler) process.exit(1)
