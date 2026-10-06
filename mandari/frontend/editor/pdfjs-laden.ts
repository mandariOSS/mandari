/**
 * pdf.js bei Bedarf laden: eigener Chunk samt Worker, einmal je Seite.
 *
 * Genutzt vom Briefkopf im Editor (letterhead.ts) und von der Blattansicht der Sitzungsvorbereitung
 * (frontend/alpine/pdf-blatt.ts). Schlägt das Laden fehl, liefert die Funktion `null`; die Aufrufer
 * zeigen dann einen Hinweis bzw. lassen die Vorschau weg.
 */

export type PdfJsModul = typeof import('pdfjs-dist')

let laden: Promise<PdfJsModul | null> | null = null

export function ladePdfJs(): Promise<PdfJsModul | null> {
  if (!laden) {
    laden = Promise.all([import('pdfjs-dist'), import('pdfjs-dist/build/pdf.worker.min.mjs?url')])
      .then(([pdfjsLib, worker]) => {
        pdfjsLib.GlobalWorkerOptions.workerSrc = worker.default
        return pdfjsLib
      })
      .catch((err) => {
        console.warn('PDF.js konnte nicht geladen werden:', err)
        // Beim nächsten Aufruf erneut versuchen (z. B. nach einem kurzen Netzaussetzer)
        laden = null
        return null
      })
  }
  return laden
}
