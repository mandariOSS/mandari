/** Wie `toPascalCase` in Lucide (`file-text` → `FileText`): Schlüssel eines Icons im Icon-Objekt. */
export function toPascalCase(name: string): string {
  let out = ''
  let upperNext = false
  for (const ch of name) {
    if (ch === '-' || ch === '_' || ch <= ' ') {
      upperNext = out.length > 0
      continue
    }
    if (out.length === 0) out += ch.toLowerCase()
    else out += upperNext ? ch.toUpperCase() : ch
    upperNext = false
  }
  return out.charAt(0).toUpperCase() + out.slice(1)
}
