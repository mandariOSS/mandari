/**
 * Tests für den Navigations-Helfer (`frontend/js/navigation.ts`).
 *
 * Ausführen mit: npm run test:navigation
 * (baut navigation.ts via esbuild nach CJS; `window` wird minimal nachgebildet)
 */

import { execSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = join(here, '..', '..', '..')
const outFile = join(here, 'build', 'navigation.cjs')

execSync(
  `npx esbuild frontend/js/navigation.ts --bundle --format=cjs --platform=node --outfile="${outFile}"`,
  { cwd: projectRoot, stdio: 'inherit' }
)

// Minimales window: nur href/origin lesen und assign aufzeichnen
let assigned = null
globalThis.window = {
  location: {
    href: 'https://app.example.org/work/documents/42/',
    origin: 'https://app.example.org',
    assign: (url) => {
      assigned = url
    },
  },
}

const require = createRequire(import.meta.url)
const { resolveSameOrigin, navigateTo } = require(outFile)

let passed = 0
let failed = 0

function test(name, fn) {
  assigned = null
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

test('Relativer Pfad wird zur absoluten Same-Origin-URL', () => {
  assert.equal(resolveSameOrigin('/work/documents/'), 'https://app.example.org/work/documents/')
})

test('Absolute URL derselben Herkunft bleibt erhalten', () => {
  assert.equal(resolveSameOrigin('https://app.example.org/x?y=1'), 'https://app.example.org/x?y=1')
})

test('javascript:-URL wird abgelehnt', () => {
  assert.equal(resolveSameOrigin('javascript:alert(1)'), null)
})

test('data:-URL wird abgelehnt', () => {
  assert.equal(resolveSameOrigin('data:text/html,<script>alert(1)</script>'), null)
})

test('Fremde Herkunft wird abgelehnt', () => {
  assert.equal(resolveSameOrigin('https://evil.example.com/phish'), null)
})

test('Protokollrelative URL auf fremde Host wird abgelehnt', () => {
  assert.equal(resolveSameOrigin('//evil.example.com/phish'), null)
})

test('Leeres Ziel und Nicht-String werden abgelehnt', () => {
  assert.equal(resolveSameOrigin(''), null)
  assert.equal(resolveSameOrigin(undefined), null)
  assert.equal(resolveSameOrigin(42), null)
})

test('navigateTo springt auf gültiges Ziel', () => {
  navigateTo('/work/documents/')
  assert.equal(assigned, 'https://app.example.org/work/documents/')
})

test('navigateTo weicht bei ungültigem Ziel auf den Fallback aus', () => {
  navigateTo('https://evil.example.com/phish')
  assert.equal(assigned, 'https://app.example.org/')
})

test('navigateTo nutzt den angegebenen Fallback', () => {
  navigateTo('javascript:alert(1)', '/accounts/login/')
  assert.equal(assigned, 'https://app.example.org/accounts/login/')
})

console.log(`\n${passed} bestanden, ${failed} fehlgeschlagen`)
if (failed > 0) process.exit(1)
