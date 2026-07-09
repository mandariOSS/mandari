/**
 * Fixture-Tests für cleanPastedHtml (Paste-Cleanup aus Word/Google Docs).
 *
 * Ausführen mit: npm run test:paste
 * (baut paste-cleanup.ts via esbuild nach CJS und prüft echte Word-/GDocs-HTML-Fixtures)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'paste-cleanup.cjs')

// paste-cleanup.ts nach CJS bauen (pragmatisch: esbuild, kein ts-node nötig)
execSync(
  `npx esbuild frontend/editor/paste-cleanup.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`,
  { cwd: projectRoot, stdio: 'inherit' }
)

const require = createRequire(import.meta.url)
const { cleanPastedHtml } = require(outFile)

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
    console.error(`       ${err.message}`)
  }
}

// ---------------------------------------------------------------------------
// Fixture 1: Word-Absatz mit mso-Styles, <o:p>, Kommentaren und Mso-Klassen
// ---------------------------------------------------------------------------
const wordParagraph = `
<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head><meta http-equiv=Content-Type content="text/html; charset=windows-1252">
<style><!-- p.MsoNormal {mso-style-parent:""; margin:0cm; font-size:11.0pt;} --></style>
</head>
<body lang=DE style='tab-interval:35.4pt'>
<!--StartFragment-->
<p class=MsoNormal style='margin-bottom:12.0pt;line-height:107%;mso-outline-level:1'>
Das ist <b style='mso-bidi-font-weight:normal'>fetter</b> und
<i>kursiver</i> Text mit <span style='color:#C00000;mso-themecolor:accent2'>Farbe</span>.<o:p></o:p></p>
<!--EndFragment-->
</body>
</html>`

test('Word-Absatz: mso-Müll entfernt, Formatierung erhalten', () => {
  const out = cleanPastedHtml(wordParagraph)
  assert.ok(!/mso-/i.test(out), 'mso-Styles müssen entfernt sein')
  assert.ok(!/<o:p>/i.test(out), '<o:p> muss entfernt sein')
  assert.ok(!/<!--/.test(out), 'Kommentare müssen entfernt sein')
  assert.ok(!/class\s*=\s*"?Mso/i.test(out), 'Mso-Klassen müssen entfernt sein')
  assert.ok(!/lang=/i.test(out), 'lang-Attribute müssen entfernt sein')
  assert.ok(/<b\b[^>]*>fetter<\/b>/.test(out), 'Fettung muss erhalten bleiben')
  assert.ok(/<i>kursiver<\/i>/.test(out), 'Kursivschrift muss erhalten bleiben')
  assert.ok(/color:\s*#C00000/i.test(out), 'Textfarbe muss erhalten bleiben')
  assert.ok(!/margin-bottom|line-height/i.test(out), 'Nicht unterstützte Styles müssen entfernt sein')
})

// ---------------------------------------------------------------------------
// Fixture 2: Word-Aufzählung (MsoListParagraph mit Symbol-Markern)
// ---------------------------------------------------------------------------
const wordBulletList = `
<p class=MsoListParagraphCxSpFirst style='text-indent:-18.0pt;mso-list:l0 level1 lfo1'><![if !supportLists]><span
style='font-family:Symbol;mso-fareast-font-family:Symbol;mso-bidi-font-family:Symbol'><span
style='mso-list:Ignore'>·<span style='font:7.0pt "Times New Roman"'>&nbsp;&nbsp;&nbsp;&nbsp;
</span></span></span><![endif]>Erster Punkt<o:p></o:p></p>
<p class=MsoListParagraphCxSpMiddle style='text-indent:-18.0pt;mso-list:l0 level1 lfo1'><![if !supportLists]><span
style='font-family:Symbol'><span style='mso-list:Ignore'>·<span style='font:7.0pt "Times New Roman"'>&nbsp;
</span></span></span><![endif]>Zweiter Punkt<o:p></o:p></p>
<p class=MsoListParagraphCxSpLast style='text-indent:-18.0pt;mso-list:l0 level2 lfo1'><![if !supportLists]><span
style='font-family:"Courier New"'><span style='mso-list:Ignore'>o<span style='font:7.0pt "Times New Roman"'>&nbsp;
</span></span></span><![endif]>Unterpunkt<o:p></o:p></p>`

test('Word-Aufzählung: MsoListParagraph wird zu <ul>/<li>', () => {
  const out = cleanPastedHtml(wordBulletList)
  assert.ok(/<ul>/.test(out), 'Es muss eine <ul> erzeugt werden')
  assert.ok(/<li>Erster Punkt/.test(out), 'Erster Punkt muss ein <li> sein')
  assert.ok(/<li>Zweiter Punkt/.test(out), 'Zweiter Punkt muss ein <li> sein')
  assert.ok(/<ul><li>Unterpunkt/.test(out.replace(/\s+/g, '')), 'Unterpunkt muss verschachtelt sein')
  assert.ok(!/·/.test(out), 'Bullet-Marker (·) darf nicht im Text landen')
  assert.ok(!/mso-list/i.test(out), 'mso-list-Styles müssen entfernt sein')
})

// ---------------------------------------------------------------------------
// Fixture 3: Word-Nummerierung (MsoListParagraph mit 1. 2. Markern)
// ---------------------------------------------------------------------------
const wordNumberedList = `
<p class=MsoListParagraph style='text-indent:-18.0pt;mso-list:l1 level1 lfo2'><![if !supportLists]><span
style='mso-fareast-font-family:Calibri'><span style='mso-list:Ignore'>1.<span
style='font:7.0pt "Times New Roman"'>&nbsp;&nbsp;&nbsp; </span></span></span><![endif]>Antrag stellen<o:p></o:p></p>
<p class=MsoListParagraph style='text-indent:-18.0pt;mso-list:l1 level1 lfo2'><![if !supportLists]><span
style='mso-fareast-font-family:Calibri'><span style='mso-list:Ignore'>2.<span
style='font:7.0pt "Times New Roman"'>&nbsp;&nbsp;&nbsp; </span></span></span><![endif]>Begründung anfügen<o:p></o:p></p>`

test('Word-Nummerierung: MsoListParagraph wird zu <ol>/<li>', () => {
  const out = cleanPastedHtml(wordNumberedList)
  assert.ok(/<ol>/.test(out), 'Es muss eine <ol> erzeugt werden')
  assert.ok(/<li>Antrag stellen/.test(out), 'Erster Eintrag muss ein <li> sein')
  assert.ok(/<li>Begründung anfügen/.test(out), 'Zweiter Eintrag muss ein <li> sein')
  assert.ok(!/1\./.test(out.replace(/<[^>]*>/g, '')), 'Nummern-Marker darf nicht im Text landen')
})

// ---------------------------------------------------------------------------
// Fixture 4: Google Docs (b-Wrapper, Span-Styles mit font-weight:700 etc.)
// ---------------------------------------------------------------------------
const gdocsHtml = `<meta charset="utf-8"><b style="font-weight:normal;" id="docs-internal-guid-1a2b3c4d-7fff-0a1b">
<p dir="ltr" style="line-height:1.38;margin-top:0pt;margin-bottom:0pt;"><span
style="font-size:11pt;font-family:Arial,sans-serif;color:#000000;background-color:transparent;font-weight:700;font-style:normal;font-variant:normal;text-decoration:none;vertical-align:baseline;white-space:pre-wrap;">Fett aus GDocs</span><span
style="font-size:11pt;font-family:Arial,sans-serif;color:#000000;font-weight:400;font-style:italic;text-decoration:none;white-space:pre-wrap;"> und kursiv</span><span
style="font-size:11pt;font-family:Arial;color:#ff0000;font-weight:400;text-decoration:underline;white-space:pre-wrap;"> rot unterstrichen</span></p>
<h2 dir="ltr" style="line-height:1.38;margin-top:18pt;margin-bottom:6pt;"><span style="font-size:16pt;font-weight:400;">Eine Überschrift</span></h2></b>`

test('Google Docs: Wrapper entfernt, Inline-Styles gefiltert, Formatierung erhalten', () => {
  const out = cleanPastedHtml(gdocsHtml)
  assert.ok(!/docs-internal-guid/.test(out), 'GDocs-Wrapper muss entfernt sein')
  assert.ok(!/<meta/i.test(out), '<meta> muss entfernt sein')
  assert.ok(/font-weight:\s*700/.test(out), 'font-weight:700 (fett) muss erhalten bleiben')
  assert.ok(/font-style:\s*italic/.test(out), 'font-style:italic muss erhalten bleiben')
  assert.ok(/text-decoration:\s*underline/.test(out), 'Unterstreichung muss erhalten bleiben')
  assert.ok(/color:\s*#ff0000/.test(out), 'Textfarbe muss erhalten bleiben')
  assert.ok(/<h2[^>]*>/.test(out), 'Überschrift muss erhalten bleiben')
  assert.ok(!/font-family|font-size|white-space|vertical-align|line-height/i.test(out),
    'Nicht unterstützte Styles müssen entfernt sein')
  assert.ok(!/font-weight:\s*400/.test(out), 'font-weight:400 (kein Format) muss entfernt sein')
})

// ---------------------------------------------------------------------------
// Fixture 5: Word-Tabelle + Überschrift + leere Absätze am Ende
// ---------------------------------------------------------------------------
const wordTable = `
<h1 style='mso-outline-level:1'><span style='mso-fareast-font-family:"Times New Roman"'>Bericht</span></h1>
<table class=MsoTableGrid border=1 cellspacing=0 cellpadding=0 style='border-collapse:collapse;border:none;mso-border-alt:solid windowtext .5pt;mso-yfti-tbllook:1184'>
 <tr style='mso-yfti-irow:0;mso-yfti-firstrow:yes'>
  <td width=301 valign=top style='width:225.4pt;border:solid windowtext 1.0pt;mso-border-alt:solid windowtext .5pt;padding:0cm 5.4pt 0cm 5.4pt'>
  <p class=MsoNormal align=center style='text-align:center'><b>Spalte A</b><o:p></o:p></p>
  </td>
  <td width=301 valign=top style='width:225.4pt;border:solid windowtext 1.0pt;padding:0cm 5.4pt'>
  <p class=MsoNormal><b>Spalte B</b><o:p></o:p></p>
  </td>
 </tr>
 <tr style='mso-yfti-irow:1;mso-yfti-lastrow:yes'>
  <td width=301 valign=top style='width:225.4pt;padding:0cm 5.4pt'>
  <p class=MsoNormal>Wert 1<o:p></o:p></p>
  </td>
  <td width=301 valign=top style='width:225.4pt;padding:0cm 5.4pt'>
  <p class=MsoNormal>Wert 2<o:p></o:p></p>
  </td>
 </tr>
</table>
<p class=MsoNormal><o:p>&nbsp;</o:p></p>
<p class=MsoNormal><span style='mso-spacerun:yes'>&nbsp;</span><o:p></o:p></p>`

test('Word-Tabelle: Struktur erhalten, Müll entfernt, leere End-Absätze weg', () => {
  const out = cleanPastedHtml(wordTable)
  assert.ok(/<h1[^>]*>/.test(out), 'Überschrift muss erhalten bleiben')
  assert.ok(/<table/.test(out) && /<tr/.test(out) && /<td/.test(out), 'Tabellenstruktur muss erhalten bleiben')
  assert.ok(/Spalte A/.test(out) && /Wert 2/.test(out), 'Zellinhalte müssen erhalten bleiben')
  assert.ok(/text-align:\s*center/.test(out), 'Zentrierung muss erhalten bleiben')
  assert.ok(!/mso-/i.test(out), 'mso-Styles müssen entfernt sein')
  assert.ok(!/windowtext/i.test(out), 'windowtext-Werte müssen entfernt sein')
  assert.ok(!/<p[^>]*>(\s|&nbsp;)*<\/p>\s*$/.test(out), 'Leere Absätze am Ende müssen entfernt sein')
  assert.ok(/Wert 2/.test(out.split('</table>')[0]), 'Inhalte dürfen nicht verschoben werden')
})

// ---------------------------------------------------------------------------
console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
if (failed > 0) process.exit(1)
