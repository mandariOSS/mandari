/**
 * Bereinigte Darstellung von KI-Antworten (KI-Assistent im Bürgerportal, KI-Hilfe im Work-Editor).
 *
 * KI-Antworten können Text aus Ratsdokumenten wiedergeben und sind deshalb wie fremdes HTML zu behandeln.
 * Die Ausgabe wird aus einem inerten Dokument (DOMParser lädt keine Bilder und führt nichts aus) Element für
 * Element neu aufgebaut – nur eine enge Positivliste an Elementen ohne beliebige Attribute:
 *
 * - erlaubt: Absätze, Überschriften, Listen, Hervorhebungen, Code, Zitate, Tabellen, Links
 * - Links nur auf die eigene Seite (relativ oder gleicher Ursprung) oder auf `https`; andere Ziele
 *   (`javascript:`, `data:`, `http:` fremder Seiten …) verlieren den Link, der Text bleibt.
 *   Externe Links bekommen `rel="noopener noreferrer"` und öffnen in einem neuen Tab.
 * - Inhalt von `script`, `style`, `iframe`, `svg`, `math` & Co. entfällt ganz; andere unbekannte Elemente
 *   (z. B. `img`) fallen weg, ihr Text bleibt.
 */

const ERLAUBT = new Set([
  'p',
  'br',
  'hr',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'strong',
  'b',
  'em',
  'i',
  'u',
  's',
  'del',
  'ul',
  'ol',
  'li',
  'blockquote',
  'pre',
  'code',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
  'a',
  'span',
  'div',
])

/** Elemente, deren Inhalt nie als Text erscheinen soll. */
const MIT_INHALT_VERWERFEN = new Set([
  'script',
  'style',
  'iframe',
  'frame',
  'frameset',
  'object',
  'embed',
  'template',
  'noscript',
  'noembed',
  'noframes',
  'svg',
  'math',
  'textarea',
  'select',
  'title',
  'head',
  'xmp',
  'plaintext',
])

const AUSRICHTUNG = new Set(['left', 'center', 'right'])

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * Prüft ein Linkziel: eigene Seite (relativ oder gleicher Ursprung) oder `https`.
 * Gibt das normalisierte Ziel und ob es extern ist zurück, sonst `null`.
 */
export function pruefeLink(
  href: string,
  basis: string = window.location.href,
): { url: string; extern: boolean } | null {
  const roh = href.trim()
  if (!roh) return null
  let url: URL
  try {
    url = new URL(roh, basis)
  } catch {
    return null
  }
  if (url.username || url.password) return null
  const eigen = new URL(basis).origin
  if (url.origin === eigen && (url.protocol === 'https:' || url.protocol === 'http:')) {
    return { url: url.href, extern: false } // absolut: ein Pfad wie "//host" wäre sonst fremd
  }
  if (url.protocol === 'https:') return { url: url.href, extern: true }
  return null
}

function kopiere(quelle: Node, ziel: Node, doc: Document): void {
  for (const kind of Array.from(quelle.childNodes)) {
    if (kind.nodeType === Node.TEXT_NODE) {
      ziel.appendChild(doc.createTextNode(kind.textContent ?? ''))
      continue
    }
    if (kind.nodeType !== Node.ELEMENT_NODE) continue // Kommentare, CDATA, Verarbeitungsanweisungen
    const element = kind as Element
    // Nur HTML-Namensraum; SVG/MathML (auch verschachtelt) entfallen ganz
    if (element.namespaceURI !== 'http://www.w3.org/1999/xhtml') continue
    const tag = element.localName
    if (MIT_INHALT_VERWERFEN.has(tag)) continue
    if (!ERLAUBT.has(tag)) {
      kopiere(element, ziel, doc) // Element weg, Text bleibt
      continue
    }
    if (tag === 'a') {
      const link = pruefeLink(element.getAttribute('href') ?? '')
      if (!link) {
        kopiere(element, ziel, doc)
        continue
      }
      const a = doc.createElement('a')
      a.setAttribute('href', link.url)
      if (link.extern) {
        a.setAttribute('rel', 'noopener noreferrer')
        a.setAttribute('target', '_blank')
      }
      kopiere(element, a, doc)
      ziel.appendChild(a)
      continue
    }
    const neu = doc.createElement(tag)
    if (tag === 'ol') {
      const start = element.getAttribute('start') ?? ''
      if (/^\d{1,6}$/.test(start)) neu.setAttribute('start', start)
    }
    if (tag === 'th' || tag === 'td') {
      const ausrichtung = (element.getAttribute('align') ?? '').toLowerCase()
      if (AUSRICHTUNG.has(ausrichtung)) neu.setAttribute('align', ausrichtung)
    }
    kopiere(element, neu, doc)
    ziel.appendChild(neu)
  }
}

/** Bereinigt HTML aus einer KI-Antwort auf die Positivliste (siehe Moduldoku). */
export function bereinigeKiHtml(html: string | null | undefined): string {
  if (!html) return ''
  if (typeof DOMParser === 'undefined') return escapeHtml(String(html))
  const quelle = new DOMParser().parseFromString(String(html), 'text/html')
  const doc = document.implementation.createHTMLDocument('')
  const behaelter = doc.createElement('div')
  kopiere(quelle.body, behaelter, doc)
  return behaelter.innerHTML
}

interface MarkedGlobal {
  parse: (text: string, optionen: Record<string, unknown>) => string
}

/** Markdown einer KI-Antwort als bereinigtes HTML (marked aus static/vendor, sonst Text mit Umbrüchen). */
export function kiMarkdownHtml(text: string | null | undefined): string {
  if (!text) return ''
  const marked = (window as unknown as { marked?: MarkedGlobal }).marked
  let roh: string
  try {
    roh = marked ? marked.parse(String(text), { breaks: true, gfm: true }) : ''
  } catch {
    roh = ''
  }
  if (!roh) roh = escapeHtml(String(text)).replace(/\n/g, '<br>')
  return bereinigeKiHtml(roh)
}
