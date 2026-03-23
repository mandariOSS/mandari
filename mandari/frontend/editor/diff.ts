/**
 * Diff rendering utility for version history.
 *
 * Compares two HTML documents paragraph-by-paragraph and produces
 * a formatted diff with <ins> (green) and <del> (red) inline markers,
 * preserving the document's block structure (headings, paragraphs, lists).
 */

import { diffWords } from 'diff'

/**
 * Extract structured text blocks from HTML, preserving block-level semantics.
 * Each block retains its tag type so the diff output keeps the original formatting.
 */
interface TextBlock {
  tag: string      // 'p', 'h1', 'h2', 'li', 'blockquote', etc.
  text: string     // Plain text content of the block
  attrs: string    // Original tag attributes (class, style, etc.)
}

function htmlToBlocks(html: string): TextBlock[] {
  const div = document.createElement('div')
  div.innerHTML = html
  const blocks: TextBlock[] = []

  function extractBlocks(el: Element) {
    for (const child of Array.from(el.children)) {
      const tag = child.tagName.toLowerCase()

      // Block-level elements → extract as blocks
      if (['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'tr'].includes(tag)) {
        const text = (child.textContent || '').trim()
        if (text) {
          // Preserve class/style attributes for rendering
          const attrs = child.getAttribute('class') ? ` class="${child.getAttribute('class')}"` : ''
          blocks.push({ tag, text, attrs })
        }
      } else if (['ul', 'ol', 'table', 'tbody', 'thead', 'div', 'section'].includes(tag)) {
        // Container elements → recurse
        extractBlocks(child)
      } else {
        // Inline or unknown → treat as paragraph
        const text = (child.textContent || '').trim()
        if (text) {
          blocks.push({ tag: 'p', text, attrs: '' })
        }
      }
    }
  }

  extractBlocks(div)

  // Fallback: if no blocks extracted (plain text without HTML tags), split by newlines
  if (blocks.length === 0) {
    const plainText = div.textContent || ''
    for (const line of plainText.split(/\n+/)) {
      const trimmed = line.trim()
      if (trimmed) {
        blocks.push({ tag: 'p', text: trimmed, attrs: '' })
      }
    }
  }

  return blocks
}

/**
 * Diff two text strings at word level, producing inline HTML with <ins>/<del>.
 */
function diffInline(oldText: string, newText: string): string {
  if (oldText === newText) return escapeHtml(oldText)

  const changes = diffWords(oldText, newText)
  let html = ''

  for (const part of changes) {
    const escaped = escapeHtml(part.value)
    if (part.added) {
      html += `<ins class="diff-added">${escaped}</ins>`
    } else if (part.removed) {
      html += `<del class="diff-removed">${escaped}</del>`
    } else {
      html += escaped
    }
  }

  return html
}

/**
 * Render a visual diff between two HTML strings.
 *
 * Preserves document structure (headings, paragraphs, lists) and shows
 * word-level changes inline with colored <ins>/<del> markers.
 *
 * @param oldHtml - The original/revision HTML content
 * @param newHtml - The new/current HTML content
 * @returns HTML string with structure-preserving diff
 */
export function renderDiff(oldHtml: string, newHtml: string): string {
  const oldBlocks = htmlToBlocks(oldHtml)
  const newBlocks = htmlToBlocks(newHtml)

  // Simple LCS-like block alignment: match blocks by similarity
  const result: string[] = []
  let oldIdx = 0
  let newIdx = 0

  while (oldIdx < oldBlocks.length || newIdx < newBlocks.length) {
    const oldBlock = oldBlocks[oldIdx]
    const newBlock = newBlocks[newIdx]

    if (!oldBlock && newBlock) {
      // Block only in new version → fully added
      const content = `<ins class="diff-added">${escapeHtml(newBlock.text)}</ins>`
      result.push(`<${newBlock.tag}${newBlock.attrs}>${content}</${newBlock.tag}>`)
      newIdx++
    } else if (oldBlock && !newBlock) {
      // Block only in old version → fully removed
      const content = `<del class="diff-removed">${escapeHtml(oldBlock.text)}</del>`
      result.push(`<${oldBlock.tag}${oldBlock.attrs}>${content}</${oldBlock.tag}>`)
      oldIdx++
    } else if (oldBlock && newBlock) {
      // Both exist → check if they're similar enough to diff inline
      const similarity = textSimilarity(oldBlock.text, newBlock.text)

      if (similarity > 0.3) {
        // Similar enough → inline word diff
        const tag = newBlock.tag
        const attrs = newBlock.attrs
        const inlineDiff = diffInline(oldBlock.text, newBlock.text)
        result.push(`<${tag}${attrs}>${inlineDiff}</${tag}>`)
        oldIdx++
        newIdx++
      } else {
        // Too different → look ahead to find a better match
        const lookAhead = findBestMatch(oldBlock.text, newBlocks, newIdx, 5)
        if (lookAhead > newIdx) {
          // Insert new blocks before the match
          for (let i = newIdx; i < lookAhead; i++) {
            const content = `<ins class="diff-added">${escapeHtml(newBlocks[i].text)}</ins>`
            result.push(`<${newBlocks[i].tag}${newBlocks[i].attrs}>${content}</${newBlocks[i].tag}>`)
          }
          newIdx = lookAhead
        } else {
          // No match found → show old as removed, new as added
          const removedContent = `<del class="diff-removed">${escapeHtml(oldBlock.text)}</del>`
          result.push(`<${oldBlock.tag}${oldBlock.attrs}>${removedContent}</${oldBlock.tag}>`)
          const addedContent = `<ins class="diff-added">${escapeHtml(newBlock.text)}</ins>`
          result.push(`<${newBlock.tag}${newBlock.attrs}>${addedContent}</${newBlock.tag}>`)
          oldIdx++
          newIdx++
        }
      }
    }
  }

  return result.join('\n')
}

/**
 * Simple text similarity score (0..1) based on shared words.
 */
function textSimilarity(a: string, b: string): number {
  const wordsA = new Set(a.toLowerCase().split(/\s+/))
  const wordsB = new Set(b.toLowerCase().split(/\s+/))
  if (wordsA.size === 0 && wordsB.size === 0) return 1
  let shared = 0
  for (const w of wordsA) {
    if (wordsB.has(w)) shared++
  }
  return shared / Math.max(wordsA.size, wordsB.size)
}

/**
 * Look ahead in newBlocks to find the best match for oldText.
 */
function findBestMatch(oldText: string, newBlocks: TextBlock[], startIdx: number, range: number): number {
  let bestIdx = startIdx
  let bestScore = 0
  const end = Math.min(startIdx + range, newBlocks.length)
  for (let i = startIdx; i < end; i++) {
    const score = textSimilarity(oldText, newBlocks[i].text)
    if (score > bestScore && score > 0.5) {
      bestScore = score
      bestIdx = i
    }
  }
  return bestScore > 0.5 ? bestIdx : startIdx
}

function escapeHtml(text: string): string {
  const map: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  }
  return text.replace(/[&<>"']/g, (m) => map[m])
}
