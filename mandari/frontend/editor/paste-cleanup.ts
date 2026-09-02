/**
 * Paste-Cleanup — bereinigt HTML aus Word/Google Docs beim Einfügen.
 *
 * Reine String-Funktion ohne DOM-Abhängigkeit, damit sie sowohl im
 * Browser (transformPastedHTML) als auch in Node-Tests läuft.
 *
 * Entfernt: mso-Styles, <o:p>/Namespace-Tags, Mso*-Klassen, Kommentar-Reste,
 * nicht unterstützte style-Attribute, leere Absätze am Ende.
 * Erhält: Überschriften, Listen (inkl. Konvertierung von Word-Listen-Absätzen
 * zu echten <ul>/<ol>), Tabellen sowie die unterstützten Inline-Styles
 * (bold/italic/underline/strike/color/highlight/text-align).
 */

/** Erlaubte CSS-Eigenschaften in style-Attributen. */
const ALLOWED_STYLE_PROPS = new Set([
  'font-weight',
  'font-style',
  'text-decoration',
  'text-decoration-line',
  'color',
  'background-color',
  'text-align',
])

/** Werte, die effektiv "kein Format" bedeuten und entfernt werden. */
const NOOP_STYLE_VALUES: Record<string, Set<string>> = {
  'font-weight': new Set(['normal', '400']),
  'font-style': new Set(['normal']),
  'text-decoration': new Set(['none']),
  'text-decoration-line': new Set(['none']),
  'text-align': new Set(['start']),
}

interface WordListItem {
  level: number
  ordered: boolean
  content: string
}

/** Entities/Tags aus einem HTML-Schnipsel entfernen (für Marker-Erkennung). */
function toPlainText(html: string): string {
  // Bis zum Fixpunkt wiederholen, damit auch verschachtelte Reste wie
  // "<<span>span>" keine Tag-Fragmente übrig lassen
  let out = html
  let prev = ''
  let guard = 0
  while (out !== prev && guard < 10) {
    prev = out
    out = out.replace(/<[^>]*>/g, '')
    guard++
  }
  return out
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .trim()
}

/**
 * Entfernt den Word-Listenmarker (<span style="mso-list:Ignore">…</span>)
 * balanciert aus dem Inhalt und gibt Markertext + bereinigten Inhalt zurück.
 */
function extractListMarker(content: string): { content: string; marker: string } {
  const openMatch = /<span\b[^>]*mso-list\s*:\s*ignore[^>]*>/i.exec(content)
  if (!openMatch) return { content, marker: '' }

  const start = openMatch.index
  const scanner = /<span\b[^>]*>|<\/span\s*>/gi
  scanner.lastIndex = start + openMatch[0].length
  let depth = 1
  let end = content.length
  let m: RegExpExecArray | null
  while ((m = scanner.exec(content)) !== null) {
    depth += m[0][1] === '/' ? -1 : 1
    if (depth === 0) {
      end = m.index + m[0].length
      break
    }
  }

  const markerHtml = content.slice(start, end)
  const cleaned = content.slice(0, start) + content.slice(end)
  return { content: cleaned, marker: toPlainText(markerHtml) }
}

/** Erkennung: ist der Word-Listenmarker eine Nummerierung? */
function isOrderedMarker(marker: string): boolean {
  return /^\(?([0-9]+|[a-zA-Z]|[ivxlcdmIVXLCDM]+)[.)]/.test(marker.trim())
}

/** Rekursiver Aufbau verschachtelter Listen aus Word-Listen-Absätzen. */
function buildList(items: WordListItem[], start: number, level: number): { html: string; next: number } {
  const ordered = items[start].ordered
  const tag = ordered ? 'ol' : 'ul'
  let html = `<${tag}>`
  let i = start
  while (i < items.length && items[i].level >= level) {
    if (items[i].level === level && items[i].ordered !== ordered) break
    if (items[i].level > level) {
      // Tiefere Ebene ohne vorangehendes Item auf dieser Ebene — einfach einhängen
      const sub = buildList(items, i, items[i].level)
      html += `<li>${sub.html}</li>`
      i = sub.next
      continue
    }
    let li = `<li>${items[i].content}`
    i++
    if (i < items.length && items[i].level > level) {
      const sub = buildList(items, i, items[i].level)
      li += sub.html
      i = sub.next
    }
    html += li + '</li>'
  }
  html += `</${tag}>`
  return { html, next: i }
}

/**
 * Konvertiert aufeinanderfolgende Word-Listen-Absätze
 * (<p class="MsoListParagraph…" style="…mso-list:l0 level1…">) in <ul>/<ol>.
 */
function convertWordLists(html: string): string {
  const pRegex = /<p\b[^>]*>[\s\S]*?<\/p\s*>/gi
  let result = ''
  let lastIndex = 0
  let pending: WordListItem[] = []

  const flush = () => {
    if (pending.length === 0) return
    let i = 0
    while (i < pending.length) {
      const built = buildList(pending, i, pending[i].level)
      result += built.html
      i = built.next
    }
    pending = []
  }

  let match: RegExpExecArray | null
  while ((match = pRegex.exec(html)) !== null) {
    const between = html.slice(lastIndex, match.index)
    if (between.trim()) {
      // Echter Inhalt zwischen Absätzen: offene Liste abschließen
      flush()
      result += between
    } else if (pending.length === 0) {
      // Whitespace außerhalb einer Liste erhalten, innerhalb verwerfen
      result += between
    }
    lastIndex = match.index + match[0].length

    const full = match[0]
    const openTagEnd = full.indexOf('>')
    const attrs = full.slice(0, openTagEnd + 1)
    const inner = full.slice(openTagEnd + 1, full.length - full.match(/<\/p\s*>$/i)![0].length)

    const isListPara = /mso-list\s*:/i.test(attrs) || /class\s*=\s*["']?[^"'>]*MsoListParagraph/i.test(attrs)
    if (!isListPara) {
      flush()
      result += full
      continue
    }

    const levelMatch = /mso-list\s*:[^;"']*\blevel(\d+)/i.exec(attrs)
    const level = levelMatch ? parseInt(levelMatch[1], 10) : 1
    const { content, marker } = extractListMarker(inner)
    pending.push({
      level,
      ordered: isOrderedMarker(marker),
      content: content.replace(/^(\s|&nbsp;)+/i, ''),
    })
  }
  flush()
  result += html.slice(lastIndex)
  return result
}

/** style-Attribut filtern: nur unterstützte Eigenschaften behalten. */
function filterStyleAttributes(html: string): string {
  return html.replace(
    /\s+style\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi,
    (_full, dq: string | undefined, sq: string | undefined, bare: string | undefined) => {
      const raw = dq ?? sq ?? bare ?? ''
      const kept: string[] = []
      for (const decl of raw.split(';')) {
        const colon = decl.indexOf(':')
        if (colon === -1) continue
        const prop = decl.slice(0, colon).trim().toLowerCase()
        const value = decl.slice(colon + 1).trim()
        if (!ALLOWED_STYLE_PROPS.has(prop) || !value) continue
        const noop = NOOP_STYLE_VALUES[prop]
        if (noop && noop.has(value.toLowerCase())) continue
        // Word-Sonderwerte wie "windowtext" überspringen
        if (prop === 'color' && value.toLowerCase() === 'windowtext') continue
        kept.push(`${prop}: ${value}`)
      }
      return kept.length ? ` style="${kept.join('; ')}"` : ''
    }
  )
}

/** align-Attribute in text-align-Styles umwandeln (vor der Style-Filterung). */
function convertAlignAttributes(html: string): string {
  return html.replace(
    /<(p|h[1-6]|div|td|th)\b([^>]*)\salign\s*=\s*["']?(left|center|right|justify)["']?([^>]*)>/gi,
    (_full, tag: string, pre: string, align: string, post: string) => {
      const rest = pre + post
      if (/\bstyle\s*=/i.test(rest)) {
        // vorhandenes style-Attribut ergänzen
        const merged = rest.replace(
          /style\s*=\s*(?:"([^"]*)"|'([^']*)')/i,
          (_s, d: string | undefined, s2: string | undefined) => `style="${(d ?? s2 ?? '').replace(/;?\s*$/, '')};text-align:${align}"`
        )
        return `<${tag}${merged}>`
      }
      return `<${tag}${rest} style="text-align:${align}">`
    }
  )
}

/** Attribute entfernen, die nur Rauschen sind (Mso-Klassen, lang, v:, w:, o:). */
function stripNoiseAttributes(html: string): string {
  return (
    html
      // Klassen mit Mso*-Anteil komplett entfernen
      .replace(/\s+class\s*=\s*(?:"[^"]*Mso[^"]*"|'[^']*Mso[^']*'|Mso[^\s>]*)/gi, '')
      // lang- und dir-Attribute
      .replace(/\s+(?:lang|dir)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
      // Word-Namespace-Attribute (o:..., v:..., w:..., xmlns...)
      .replace(/\s+(?:xmlns|o|v|w):[a-z-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
  )
}

/** Spans ohne Attribute entfernen (Word/GDocs erzeugen tiefe Span-Nester). */
function unwrapPlainSpans(html: string): string {
  let prev = ''
  let out = html
  let guard = 0
  while (out !== prev && guard < 25) {
    prev = out
    out = out.replace(/<span\s*>([^<]*(?:<(?!\/?span\b)[^<]*)*)<\/span\s*>/gi, '$1')
    guard++
  }
  return out
}

/** Leere Absätze am Ende des Inhalts entfernen. */
function stripTrailingEmptyParagraphs(html: string): string {
  // Bewusst ohne verschachtelte Quantifizierer über Alternativen (ReDoS):
  // letzten <p>…</p>-Block per Index suchen und dessen Inhalt separat prüfen.
  let out = html.replace(/\s+$/, '')
  let guard = 0
  while (guard < 50) {
    const start = out.toLowerCase().lastIndexOf('<p')
    if (start === -1) break
    const tail = /^<p\b[^>]*>([\s\S]*)<\/p\s*>$/i.exec(out.slice(start))
    if (!tail) break
    const inner = tail[1]
      .replace(/<br\s*\/?\s*>/gi, '')
      .replace(/<span\b[^>]*>\s*<\/span\s*>/gi, '')
      .replace(/&nbsp;/gi, ' ')
    if (inner.trim() !== '') break
    out = out.slice(0, start).replace(/\s+$/, '')
    guard++
  }
  return out
}

/**
 * Bereinigt eingefügtes HTML aus Word/Google Docs.
 * Sicher für normales HTML — unbekannte, saubere Auszeichnung bleibt erhalten.
 */
export function cleanPastedHtml(html: string): string {
  if (!html) return html
  let out = html

  // Nur den Body-Inhalt betrachten, falls ein komplettes Dokument eingefügt wurde
  const bodyMatch = /<body\b[^>]*>([\s\S]*?)<\/body>/i.exec(out)
  if (bodyMatch) out = bodyMatch[1]

  // Word-XML/Metadaten-Blöcke, Kommentare & Conditional Comments entfernen.
  // Bis zum Fixpunkt wiederholen, damit durch das Entfernen keine neuen
  // "<script"/"<!--"-Fragmente aus verschachtelten Resten entstehen.
  {
    let prev = ''
    let guard = 0
    while (out !== prev && guard < 10) {
      prev = out
      out = out
        .replace(/<(script|style|xml|head|title)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '')
        .replace(/<(?:meta|link)\b[^>]*\/?>/gi, '')
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/<!\[if\s[^\]]*\]>/gi, '')
        .replace(/<!\[endif\]>/gi, '')
      guard++
    }
  }

  // Google-Docs-Wrapper: <b style="font-weight:normal" id="docs-internal-guid-…">…</b>
  const gdocsWrapper = /^\s*<b\b[^>]*docs-internal-guid[^>]*>([\s\S]*)<\/b>\s*$/i.exec(out)
  if (gdocsWrapper) out = gdocsWrapper[1]

  // Word-Listen-Absätze in echte Listen konvertieren (braucht noch die mso-Styles)
  out = convertWordLists(out)

  // <o:p>-Elemente (samt Inhalt, meist &nbsp;) und andere Namespace-Tags entfernen
  out = out
    .replace(/<o:p\b[^>]*>[\s\S]*?<\/o:p\s*>/gi, '')
    .replace(/<\/?(?:o|w|m|v|st1|st2)\s*:[^>]*>/gi, '')

  // align-Attribute konservieren, dann Styles filtern und Rausch-Attribute entfernen
  out = convertAlignAttributes(out)
  out = filterStyleAttributes(out)
  out = stripNoiseAttributes(out)

  // Übrig gebliebene nackte Spans auflösen, leere End-Absätze entfernen
  out = unwrapPlainSpans(out)
  out = stripTrailingEmptyParagraphs(out)

  return out.trim()
}

export default cleanPastedHtml
