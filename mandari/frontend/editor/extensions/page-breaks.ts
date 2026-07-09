/**
 * TipTap Page Break Extension
 *
 * Inserts visual page gap elements (widget decorations) at A4 page
 * boundaries. Gaps are placed between block-level elements so they
 * never cut through text. Uses ProseMirror's Decoration system.
 *
 * Height calculation uses actual DOM positions (getBoundingClientRect)
 * to correctly handle CSS margin collapsing between adjacent blocks.
 * Padding is read from the computed style of the editor element so it
 * adapts automatically when letterhead margins are applied.
 */

import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { EditorView } from '@tiptap/pm/view'

export interface PageBreakOptions {
  /** Page height in mm (default: 297 for A4) */
  pageHeightMm: number
  /** Height of the gap widget in px */
  gapHeight: number
  /** Callback when page count changes */
  onPageCount?: (count: number, currentPage: number) => void
}

const PAGE_BREAK_KEY = new PluginKey('pageBreaks')
const PAGE_BREAK_META = 'pageBreakDecorations'

/** Measure page height in px. Cached per session. */
let _cachedPagePx: number | null = null
function getPagePx(mm: number): number {
  if (_cachedPagePx !== null) return _cachedPagePx
  const el = document.createElement('div')
  el.style.cssText = `position:absolute;visibility:hidden;width:0;height:${mm}mm;`
  document.body.appendChild(el)
  _cachedPagePx = el.getBoundingClientRect().height
  document.body.removeChild(el)
  return _cachedPagePx
}

/** Create the DOM element for a page-gap widget. */
function createGapWidget(gapHeight: number): HTMLElement {
  const wrapper = document.createElement('div')
  wrapper.className = 'page-gap'
  wrapper.setAttribute('contenteditable', 'false')
  wrapper.style.cssText = `
    height: ${gapHeight}px;
    margin: 0;
    position: relative;
    user-select: none;
    pointer-events: none;
  `

  // Dashed separator line at center
  const line = document.createElement('div')
  line.className = 'page-gap-line'
  line.style.cssText = `
    position: absolute;
    top: 50%;
    left: 0;
    right: 0;
    height: 0;
    border-top: 1px dashed rgba(0,0,0,0.15);
  `
  wrapper.appendChild(line)

  // Top shadow (bottom edge of previous page)
  const topShadow = document.createElement('div')
  topShadow.style.cssText = `
    position: absolute;
    top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(to bottom, rgba(0,0,0,0.03), transparent);
  `
  wrapper.appendChild(topShadow)

  // Bottom shadow (top edge of next page)
  const bottomShadow = document.createElement('div')
  bottomShadow.style.cssText = `
    position: absolute;
    bottom: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(to top, rgba(0,0,0,0.03), transparent);
  `
  wrapper.appendChild(bottomShadow)

  // Label — centered on the dashed line
  const label = document.createElement('span')
  label.className = 'page-gap-label'
  label.textContent = 'Seitenumbruch'
  label.style.cssText = `
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    font-size: 9px;
    color: #b0aead;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    background: inherit;
    padding: 0 10px;
  `
  wrapper.appendChild(label)

  return wrapper
}

export const PageBreaks = Extension.create<PageBreakOptions>({
  name: 'pageBreaks',

  addOptions() {
    return {
      pageHeightMm: 297,
      gapHeight: 32,
      onPageCount: undefined,
    }
  },

  addProseMirrorPlugins() {
    const extensionOptions = this.options

    return [
      new Plugin({
        key: PAGE_BREAK_KEY,

        state: {
          init() {
            return DecorationSet.empty
          },
          apply(tr, oldSet) {
            const meta = tr.getMeta(PAGE_BREAK_META)
            if (meta !== undefined) return meta
            if (tr.docChanged) return DecorationSet.empty
            return oldSet.map(tr.mapping, tr.doc)
          },
        },

        props: {
          decorations(state) {
            return PAGE_BREAK_KEY.getState(state)
          },
        },

        view(editorView: EditorView) {
          let lastContentHash = ''
          let rafId: number | null = null
          let lastTotalPages = 1
          let gapPositions: number[] = []

          function recalculate() {
            rafId = null

            const doc = editorView.state.doc
            const contentHash = `${doc.content.size}:${doc.childCount}`
            if (contentHash === lastContentHash) {
              firePageCountCallback(editorView)
              return
            }
            lastContentHash = contentHash

            const pagePx = getPagePx(extensionOptions.pageHeightMm)
            const tiptapEl = editorView.dom as HTMLElement

            // Read actual padding from DOM (adapts to letterhead margins)
            const cs = getComputedStyle(tiptapEl)
            const padTop = parseFloat(cs.paddingTop) || 0
            const padBottom = parseFloat(cs.paddingBottom) || 0
            const usableHeight = pagePx - padTop - padBottom

            const tiptapRect = tiptapEl.getBoundingClientRect()
            const decorations: Decoration[] = []
            let pageNum = 1
            let existingGapCount = 0
            // Content-space offset (px) where the current page starts —
            // advances at automatic AND manual page breaks.
            let pageStart = 0
            const newGapPositions: number[] = []

            // Use actual DOM positions to determine page breaks.
            // getBoundingClientRect handles CSS margin collapsing correctly.
            const children = tiptapEl.children
            for (let i = 0; i < children.length; i++) {
              const child = children[i] as HTMLElement
              if (!child) continue

              // Skip existing gap widgets from previous render
              if (child.classList.contains('page-gap')) {
                existingGapCount++
                continue
              }

              const rect = child.getBoundingClientRect()
              // Edges relative to content start (after padding), minus
              // heights of existing gap widgets → pure content position
              const contentTop =
                rect.top - tiptapRect.top - padTop - existingGapCount * extensionOptions.gapHeight
              const contentBottom =
                rect.bottom - tiptapRect.top - padTop - existingGapCount * extensionOptions.gapHeight

              // Manual page break node: force a new page after it
              if (child.hasAttribute('data-page-break')) {
                newGapPositions.push(rect.bottom - tiptapRect.top)
                try {
                  const pos = editorView.posAtDOM(child, 0)
                  const resolvedPos = doc.resolve(pos)
                  // Atom-Leaf: posAtDOM liefert die Position VOR dem Node
                  // (depth 0 auf Dokumentebene) — Ende = pos + nodeSize
                  const nodeEnd =
                    resolvedPos.depth === 0
                      ? pos + (resolvedPos.nodeAfter?.nodeSize ?? 1)
                      : resolvedPos.after(resolvedPos.depth)

                  decorations.push(
                    Decoration.widget(
                      nodeEnd,
                      () => createGapWidget(extensionOptions.gapHeight),
                      {
                        side: 1,
                        key: `page-gap-${pageNum}`,
                      }
                    )
                  )
                  pageNum++
                  pageStart = contentBottom
                } catch {
                  // posAtDOM can fail for edge cases — skip
                }
                continue
              }

              if (contentBottom - pageStart > usableHeight) {
                // This block crosses the page boundary
                const gapY = rect.top - tiptapRect.top
                newGapPositions.push(gapY)

                try {
                  const pos = editorView.posAtDOM(child, 0)
                  const resolvedPos = doc.resolve(pos)
                  const nodeStart = resolvedPos.before(resolvedPos.depth)

                  decorations.push(
                    Decoration.widget(
                      nodeStart,
                      () => createGapWidget(extensionOptions.gapHeight),
                      {
                        side: -1,
                        key: `page-gap-${pageNum}`,
                      }
                    )
                  )
                  pageNum++
                  pageStart = contentTop
                  // Do NOT increment existingGapCount — new gaps aren't in the DOM yet
                  // so they don't affect getBoundingClientRect positions of subsequent children
                } catch {
                  // posAtDOM can fail for edge cases — skip
                }
              }
            }

            lastTotalPages = pageNum
            gapPositions = newGapPositions
            const decoSet = DecorationSet.create(doc, decorations)

            const tr = editorView.state.tr.setMeta(PAGE_BREAK_META, decoSet)
            tr.setMeta('addToHistory', false)
            editorView.dispatch(tr)

            // Update min-height for the page container
            const parentEl = tiptapEl.parentElement
            if (parentEl) {
              const totalGaps = lastTotalPages > 1 ? (lastTotalPages - 1) * extensionOptions.gapHeight : 0
              parentEl.style.minHeight = (lastTotalPages * pagePx + totalGaps) + 'px'
            }

            firePageCountCallback(editorView)
          }

          function firePageCountCallback(view: EditorView) {
            if (!extensionOptions.onPageCount) return
            const tiptapEl = view.dom as HTMLElement
            const scrollContainer = tiptapEl.closest('.editor-container') as HTMLElement
            const scrollTop = scrollContainer ? scrollContainer.scrollTop : 0

            let currentPage = 1
            for (let g = 0; g < gapPositions.length; g++) {
              if (scrollTop >= gapPositions[g]) {
                currentPage = g + 2
              } else {
                break
              }
            }
            currentPage = Math.min(lastTotalPages, currentPage)
            extensionOptions.onPageCount(lastTotalPages, currentPage)
          }

          function scheduleUpdate() {
            if (rafId !== null) cancelAnimationFrame(rafId)
            rafId = requestAnimationFrame(recalculate)
          }

          scheduleUpdate()

          return {
            update() {
              scheduleUpdate()
            },
            destroy() {
              if (rafId !== null) cancelAnimationFrame(rafId)
            },
          }
        },
      }),
    ]
  },
})

export default PageBreaks
