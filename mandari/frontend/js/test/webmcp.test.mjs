/**
 * Tests für WebMCP an den Formularen (`frontend/js/webmcp.ts`, Issue #1309).
 *
 * Ausführen mit: npm run test:webmcp
 * (baut webmcp.ts via esbuild nach CJS; Dokument und Ereignisse werden minimal nachgebildet)
 */

import assert from 'node:assert/strict'
import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'webmcp.cjs')

execSync(`npx esbuild frontend/js/webmcp.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`, {
  cwd: projectRoot,
  stdio: 'inherit',
})

const { installWebmcpFormulare, istAgentAufruf } = createRequire(import.meta.url)(outFile)

const formular = (werkzeug) => ({ hasAttribute: (name) => name === 'toolname' && werkzeug })
const absenden = (agentInvoked, target) => {
  const ereignis = { agentInvoked, target, angehalten: false }
  ereignis.stopImmediatePropagation = () => {
    ereignis.angehalten = true
  }
  return ereignis
}

// Lauscher in der Capture-Phase am Dokument
const lauscher = []
installWebmcpFormulare({ addEventListener: (typ, fn, capture) => lauscher.push({ typ, fn, capture }) })
assert.equal(lauscher.length, 1)
assert.equal(lauscher[0].typ, 'submit')
assert.equal(lauscher[0].capture, true, 'vor HTMX und Alpine am Formular')

// Agent ruft ein Werkzeug auf: HTMX und Alpine kommen nicht dran, das Formular navigiert
const agent = absenden(true, formular(true))
lauscher[0].fn(agent)
assert.equal(agent.angehalten, true)

// Mensch schickt ab: alles wie bisher
const mensch = absenden(undefined, formular(true))
lauscher[0].fn(mensch)
assert.equal(mensch.angehalten, false)

// Agent an einem Formular ohne Werkzeug: unberührt
const ohneWerkzeug = absenden(true, formular(false))
lauscher[0].fn(ohneWerkzeug)
assert.equal(ohneWerkzeug.angehalten, false)

assert.equal(istAgentAufruf(absenden(true, null)), false)
assert.equal(istAgentAufruf(absenden(false, formular(true))), false)

console.log('webmcp: alle Tests bestanden')
