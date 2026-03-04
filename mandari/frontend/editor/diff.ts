/**
 * Diff rendering utility for version history.
 *
 * Uses the `diff` library to compare text content and produce HTML
 * with <ins> (green) and <del> (red) elements.
 */

import { diffWords } from 'diff'

/**
 * Strip HTML tags from a string, preserving text content.
 */
function stripHtml(html: string): string {
  const div = document.createElement('div')
  div.innerHTML = html
  return div.textContent || div.innerText || ''
}

/**
 * Render a visual diff between two HTML strings.
 *
 * @param oldHtml - The original HTML content
 * @param newHtml - The new/current HTML content
 * @returns HTML string with <ins> and <del> elements highlighting changes
 */
export function renderDiff(oldHtml: string, newHtml: string): string {
  const oldText = stripHtml(oldHtml)
  const newText = stripHtml(newHtml)

  const changes = diffWords(oldText, newText)
  let html = ''

  for (const part of changes) {
    const escaped = escapeHtml(part.value)
    if (part.added) {
      html += `<ins class="bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-200 no-underline px-0.5 rounded">${escaped}</ins>`
    } else if (part.removed) {
      html += `<del class="bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200 line-through px-0.5 rounded">${escaped}</del>`
    } else {
      html += escaped
    }
  }

  return html
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
