/**
 * Letterhead renderer — renders a PDF page as a canvas background behind the editor.
 *
 * Uses PDF.js loaded dynamically to avoid bloating the main bundle.
 * The letterhead PDF is rendered at reduced opacity behind the editor content,
 * giving a WYSIWYG preview of the final exported document.
 */

export interface LetterheadOptions {
  /** URL to the letterhead PDF file */
  pdfUrl: string
  /** Container element that wraps the editor */
  container: HTMLElement
  /** Opacity of the letterhead background (0-1, default 0.25) */
  opacity?: number
  /** Content margins from the letterhead (in mm) */
  margins?: {
    top?: number
    right?: number
    bottom?: number
    left?: number
  }
}

/**
 * Renders a letterhead PDF as a background canvas in the editor.
 *
 * @returns Cleanup function to remove the letterhead canvas
 */
export async function renderLetterhead(options: LetterheadOptions): Promise<() => void> {
  const { pdfUrl, container, opacity = 0.25, margins } = options

  // Dynamically load PDF.js from vendor path
  const pdfjsLib = await loadPdfJs()
  if (!pdfjsLib) {
    console.warn('PDF.js could not be loaded. Letterhead preview disabled.')
    return () => {}
  }

  try {
    const pdf = await pdfjsLib.getDocument(pdfUrl).promise
    const page = await pdf.getPage(1)

    // Get page dimensions for A4 rendering
    const viewport = page.getViewport({ scale: 1 })
    const containerWidth = container.offsetWidth || 794 // ~210mm at 96dpi

    // Scale to fit container width
    const scale = containerWidth / viewport.width
    const scaledViewport = page.getViewport({ scale })

    // Create canvas element
    const canvas = document.createElement('canvas')
    canvas.width = scaledViewport.width
    canvas.height = scaledViewport.height
    canvas.style.position = 'absolute'
    canvas.style.top = '0'
    canvas.style.left = '50%'
    canvas.style.transform = 'translateX(-50%)'
    canvas.style.opacity = String(opacity)
    canvas.style.pointerEvents = 'none'
    canvas.style.zIndex = '0'
    canvas.className = 'letterhead-canvas'

    // Ensure container is positioned
    const containerPosition = window.getComputedStyle(container).position
    if (containerPosition === 'static') {
      container.style.position = 'relative'
    }

    // Render the PDF page to canvas
    const ctx = canvas.getContext('2d')
    if (ctx) {
      await page.render({
        canvasContext: ctx,
        viewport: scaledViewport,
      }).promise
    }

    // Insert canvas as first child (behind editor content)
    container.insertBefore(canvas, container.firstChild)

    // Apply margins to the editor's TipTap element
    if (margins) {
      const tiptapEl = container.querySelector('.tiptap') as HTMLElement
      if (tiptapEl) {
        if (margins.top) tiptapEl.style.paddingTop = `${margins.top}mm`
        if (margins.right) tiptapEl.style.paddingRight = `${margins.right}mm`
        if (margins.bottom) tiptapEl.style.paddingBottom = `${margins.bottom}mm`
        if (margins.left) tiptapEl.style.paddingLeft = `${margins.left}mm`
      }
    }

    // Return cleanup function
    return () => {
      canvas.remove()
    }
  } catch (error) {
    console.error('Error rendering letterhead:', error)
    return () => {}
  }
}

/**
 * Dynamically load PDF.js library.
 * Uses a script tag approach to avoid bundling the large PDF.js library.
 */
async function loadPdfJs(): Promise<any> {
  // Check if already loaded
  if ((window as any).pdfjsLib) {
    return (window as any).pdfjsLib
  }

  return new Promise((resolve) => {
    const script = document.createElement('script')
    script.src = '/static/vendor/pdfjs/pdf.min.mjs'
    script.type = 'module'

    // For ESM module, we need a different approach
    // Use dynamic import instead
    import(/* webpackIgnore: true */ '/static/vendor/pdfjs/pdf.min.mjs')
      .then((pdfjsLib) => {
        pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs'
        ;(window as any).pdfjsLib = pdfjsLib
        resolve(pdfjsLib)
      })
      .catch((err) => {
        console.warn('Failed to load PDF.js:', err)
        resolve(null)
      })
  })
}
