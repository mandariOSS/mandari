/**
 * Dokument-Import (Alpine-Komponente `documentImport`).
 *
 * Nimmt PDF- und DOCX-Dateien per Auswahl oder Ziehen an. Abgelehnte Dateien erscheinen mit
 * Grund – früher fielen sie stumm weg (etwa .odt oder .doc), und der Knopf blieb einfach
 * gesperrt. Während des Hochladens ist der Knopf gesperrt und zeigt den Fortschritt: Die
 * Texterkennung gescannter PDFs dauert einige Sekunden je Seite, ein zweiter Klick legte
 * das Dokument doppelt an (#620).
 *
 * Markup: `templates/work/motions/import.html`, Konfiguration per
 * `{{ import_config|json_script:"document-import-config" }}`.
 */

import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'

export interface DocumentImportConfig {
  maxBytes: number
  extensions: string[]
}

export interface RejectedFile {
  name: string
  reason: string
}

const MB = 1024 * 1024
const DEFAULT_CONFIG: DocumentImportConfig = { maxBytes: 25 * MB, extensions: ['.docx', '.pdf'] }
/** Formate, die sich als DOCX oder PDF speichern lassen – mit passendem Hinweis */
const CONVERTIBLE = ['.doc', '.odt', '.rtf', '.pages']

export function formatFileSize(bytes: number): string {
  if (bytes <= 0) return '0 Bytes'
  const units = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${Number.parseFloat((bytes / 1024 ** i).toFixed(2))} ${units[i]}`
}

/** Grund, aus dem eine Datei nicht importiert werden kann – oder `null`. */
export function rejectionReason(file: { name: string; size: number }, config: DocumentImportConfig): string | null {
  const name = file.name.toLowerCase()
  const dot = name.lastIndexOf('.')
  const ext = dot >= 0 ? name.slice(dot) : ''
  if (!config.extensions.includes(ext)) {
    if (CONVERTIBLE.includes(ext)) {
      return 'Dieses Format wird nicht übernommen. Bitte als DOCX oder PDF speichern und erneut wählen.'
    }
    return 'Nur PDF- und DOCX-Dateien lassen sich importieren.'
  }
  if (file.size <= 0) return 'Die Datei ist leer.'
  if (file.size > config.maxBytes) return `Die Datei ist größer als ${Math.floor(config.maxBytes / MB)} MB.`
  return null
}

/** Alpine-Komponente: `x-data="documentImport"` */
export const documentImport = defineComponent(() => {
  const config = readJsonScript<DocumentImportConfig>('document-import-config') ?? DEFAULT_CONFIG

  return {
    isDragging: false,
    submitting: false,
    selectedFiles: [] as File[],
    rejectedFiles: [] as RejectedFile[],
    visibility: 'private',

    handleFileSelect(event: Event): void {
      const input = event.target as HTMLInputElement
      if (input.files) this.addFiles(input.files)
    },

    handleDrop(event: DragEvent): void {
      this.isDragging = false
      if (event.dataTransfer?.files) this.addFiles(event.dataTransfer.files)
    },

    addFiles(fileList: FileList): void {
      this.rejectedFiles = []
      for (const file of Array.from(fileList)) {
        const reason = rejectionReason(file, config)
        if (reason) {
          this.rejectedFiles.push({ name: file.name, reason })
          continue
        }
        if (!this.selectedFiles.some((f) => f.name === file.name && f.size === file.size)) {
          this.selectedFiles.push(file)
        }
      }
      this.syncInput()
    },

    removeFile(index: number): void {
      this.selectedFiles.splice(index, 1)
      this.syncInput()
    },

    /** Das echte Dateifeld spiegelt die Auswahl (auch nach Ziehen und Entfernen). */
    syncInput(): void {
      const input = this.$refs.fileInput as HTMLInputElement | undefined
      if (!input) return
      const transfer = new DataTransfer()
      for (const file of this.selectedFiles) transfer.items.add(file)
      input.files = transfer.files
    },

    isDocx(file: File): boolean {
      return file.name.toLowerCase().endsWith('.docx')
    },

    formatFileSize,

    submitLabel(): string {
      if (this.submitting) return 'Wird importiert …'
      return this.selectedFiles.length <= 1 ? 'Importieren' : `${this.selectedFiles.length} Dateien importieren`
    },

    onSubmit(event: SubmitEvent): void {
      if (this.selectedFiles.length === 0 || this.submitting) {
        event.preventDefault()
        return
      }
      this.submitting = true
    },
  }
})
