/**
 * Tests für das Zusammenführen zweier Fassungen der Notizen (`frontend/alpine/notizen-zusammenfuehren.ts`,
 * Issue #1166): In einer Sitzung stand derselbe Satz dreimal im Protokoll, getrennt durch „Ihre gleichzeitige
 * Fassung:“, obwohl nur eine Person geschrieben hatte. Gleiche Fassungen und ältere Stände derselben Eingabe dürfen
 * keinen Konflikt mehr ergeben; echte Abweichungen stehen weiter untereinander, und es geht nie Text verloren.
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
const ziel = join(here, 'build', 'notizen-zusammenfuehren.cjs')
execSync(
  `npx esbuild frontend/alpine/notizen-zusammenfuehren.ts --bundle --format=cjs --platform=node --outfile="${ziel}"`,
  { cwd: projectRoot, stdio: 'inherit' },
)

const require = createRequire(import.meta.url)
const { zusammenfuehren, notizText, KONFLIKT_MARKE } = require(ziel)

let bestanden = 0
function pruefe(name, fn) {
  fn()
  bestanden += 1
  console.log(`  OK   ${name}`)
}

const p = (t) => `<p>${t}</p>`
const satz = 'Träger mit gleichem Auftrag/Inhalt suchen und ggf. zusammenfassen.'
const angefangen = 'Träger mit gleichem Auftrag/Inhalt suchen und ggf. zusammenfass'

console.log('Zusammenführen der Notizen')

pruefe('gleiches HTML ergibt keinen Konflikt', () => {
  assert.deepEqual(zusammenfuehren(p(satz), p(satz), ''), { html: p(satz), konflikt: false })
})

pruefe('gleicher Text mit veraltetem Stand (Fall aus der Sitzung) ergibt keine Doppelung', () => {
  const ergebnis = zusammenfuehren(p(satz), p(satz))
  assert.equal(ergebnis.konflikt, false)
  assert.equal(ergebnis.html.includes(KONFLIKT_MARKE), false)
})

pruefe('älterer eigener Stand (abgebrochenes Wort) übernimmt die neuere Fassung des Servers', () => {
  const ergebnis = zusammenfuehren(p(angefangen), p(satz), '')
  assert.deepEqual(ergebnis, { html: p(satz), konflikt: false })
})

pruefe('weitergetippt: eigene Fassung enthält den Server-Stand und gilt', () => {
  const eigene = `${p(satz)}${p('Rückmeldung bis Freitag.')}`
  assert.deepEqual(zusammenfuehren(eigene, p(satz), null), { html: eigene, konflikt: false })
})

pruefe('eigener Text unverändert gegenüber der Basis: Server gilt (auch bei Löschung auf dem Server)', () => {
  const basis = `${p('A')}${p('B')}`
  assert.deepEqual(zusammenfuehren(basis, p('A'), basis), { html: p('A'), konflikt: false })
})

pruefe('Server unverändert gegenüber der Basis: eigene Fassung gilt (auch eigene Löschung)', () => {
  const basis = `${p('A')}${p('B')}`
  assert.deepEqual(zusammenfuehren(p('B'), basis, basis), { html: p('B'), konflikt: false })
})

pruefe('nur Formatierung geändert: eigene Fassung gilt, kein Konflikt', () => {
  const eigene = `<p><strong>${satz}</strong></p>`
  assert.deepEqual(zusammenfuehren(eigene, p(satz), null), { html: eigene, konflikt: false })
})

pruefe('leere eigene Fassung übernimmt den Server, leerer Server die eigene', () => {
  assert.deepEqual(zusammenfuehren('<p></p>', p(satz), null), { html: p(satz), konflikt: false })
  assert.deepEqual(zusammenfuehren(p(satz), '', null), { html: p(satz), konflikt: false })
})

pruefe('echte Abweichung: beide Fassungen untereinander, nichts geht verloren', () => {
  const ergebnis = zusammenfuehren(p('Eigene Notiz'), p('Andere Notiz'), p('Start'))
  assert.equal(ergebnis.konflikt, true)
  assert.equal(ergebnis.html, `${p('Andere Notiz')}${KONFLIKT_MARKE}${p('Eigene Notiz')}`)
})

pruefe('Marken stapeln sich nicht, wenn der Server die zusammengeführte Fassung schon hat', () => {
  const beide = `${p('Andere Notiz')}${KONFLIKT_MARKE}${p('Eigene Notiz')}`
  assert.deepEqual(zusammenfuehren(p('Eigene Notiz'), beide, null), { html: beide, konflikt: false })
})

pruefe('kurze eigene Eingabe mitten im fremden Text fällt nicht weg (Konflikt statt verschlucken)', () => {
  const ergebnis = zusammenfuehren(p('Vertagt'), p('Antrag 4: Vertagt auf Mai, Antrag 5 abgelehnt'), '')
  assert.equal(ergebnis.konflikt, true)
  assert.ok(ergebnis.html.endsWith(p('Vertagt')))
})

pruefe('Text ohne Tags, Leerraum und Zeichen-Entitäten vereinheitlicht', () => {
  assert.equal(notizText('<p>a&nbsp;&amp;  b</p><ul><li>c</li></ul>'), 'a & b c')
  assert.equal(notizText('<p>&#228;&#xFC;</p>'), 'äü')
})

console.log(`\n${bestanden} Prüfungen bestanden`)
