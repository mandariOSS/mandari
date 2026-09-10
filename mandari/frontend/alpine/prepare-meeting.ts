/**
 * Sitzungsvorbereitung (Alpine-Komponente `preparationApp`).
 *
 * TOP-Navigation, Position/Ergebnis, private Notiz, Redebeitrag (TipTap über
 * `window.MandariEditor`), Diskussions-Thread mit WebSocket/Polling, PDF-Vorschau mit
 * Anmerkungen, Anhänge, Zusammenfassung und Autosave. Die Konfiguration kommt aus dem
 * View (`prepare_config`) per `{{ prepare_config|json_script:"prepare-config" }}`.
 *
 * Markup: `templates/work/meetings/prepare.html` und die `_prepare_*`-Partials.
 * Icons werden vom MutationObserver (frontend/js/icons.ts) nachgezogen.
 */

import type { Editor } from '@tiptap/core'
import type { FormatState } from '../editor/index'
import { defineComponent } from '../js/alpine/component'
import { confirmAction } from '../js/alpine/confirm-dialog'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

// ---- Konfiguration aus dem View -------------------------------------------------

export interface PreparedPaper {
  id: string
  name: string
  reference: string
  paperType: string
  consultationCount: number
  consultations: Array<Record<string, unknown>>
}

export interface PreparedFile {
  id: string
  name: string
  url: string
  previewUrl: string
  mimeType: string
  isPdf: boolean
  size: string
  pageCount: number
  /** Anzahl Anmerkungen (wird nach dem Laden der Anmerkungen aktualisiert) */
  annotations: number
}

export interface PreparedDocument {
  id: string
  title: string
  url: string
  type: string
  addedBy: string
  paperId: string | null
  sharedAcrossCommittees: boolean
}

export interface PreparedItem {
  id: string
  number: string
  name: string
  position: string
  isFinal: boolean
  reasoning: string
  outcome: string
  setBy: string | null
  crossPositions: Array<Record<string, unknown>>
  privateNote: string
  hasSpeechNote: boolean
  speechTitle: string
  speechContent: string
  speechDuration: number
  speechShared: boolean
  speechLinkedDocument: { id: string; title: string } | null
  sharedSpeeches: Array<{ author: string; content: string }>
  paper: PreparedPaper | null
  hasFiles: boolean
  files: PreparedFile[]
  documents: PreparedDocument[]
  documentLinks: Array<{ id: string; title: string; url: string }>
  notesCount: number
}

export interface PrepareConfig {
  orgSlug: string
  meetingId: string
  currentUser: string
  positionLabels: Record<string, string>
  orgNotes: string
  items: PreparedItem[]
  urls: {
    /** GET: HTML der Zusammenfassung */
    summary: string
  }
}

// ---- Zustandstypen ----------------------------------------------------------------

// biome-ignore lint/suspicious/noExplicitAny: Antworten der JSON-Endpunkte sind nicht schematisiert
type JsonResponse = Record<string, any>

export interface ThreadNote {
  id: string
  [key: string]: unknown
}

/** Anlage aus der Supplementary-API (Link oder Datei) */
export interface SupplementaryDoc {
  id: string
  title: string
  url?: string
  document_type?: string
  added_by?: string
  is_from_other_item?: boolean
  preview_kind?: string
  preview_id?: string
  preview_url?: string
  annotations?: number
  [key: string]: unknown
}

export interface Annotation {
  id: string
  page: number
  content: string
  created_at: string
  [key: string]: unknown
}

/** Quelle der Vorschau (RIS-Datei oder eigene Anlage) — trägt den Anmerkungs-Zähler */
interface PreviewSource {
  annotations?: number
}

interface PreviewState {
  open: boolean
  kind: string | null
  id: string | null
  title: string
  baseSrc: string
  frameSrc: string
  externalUrl: string
  source: PreviewSource | null
}

interface RealtimeMessage {
  type?: string
  event?: string
  comment?: ThreadNote
  comment_id?: string
  agenda_item_id?: string
  position?: {
    position?: string
    is_final?: boolean
    outcome?: string
    reasoning?: string
  }
}

type RealtimeMode = 'ws' | 'poll' | 'off'

const CONFIG_ID = 'prepare-config'

const POSITION_DOTS: Record<string, string> = {
  for: 'bg-green-500',
  against: 'bg-red-500',
  abstain: 'bg-gray-400',
  defer: 'bg-purple-500',
  refer: 'bg-indigo-500',
  amended: 'bg-amber-500',
  info: 'bg-blue-500',
  open: 'bg-gray-400 dark:bg-gray-500',
}

const POSITION_CHIPS: Record<string, string> = {
  for: 'bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300',
  against: 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300',
  abstain: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
  defer: 'bg-purple-50 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300',
  refer: 'bg-indigo-50 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300',
  amended: 'bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300',
  info: 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
}

const SUMMARY_ERROR = '<p class="text-sm text-red-600">Zusammenfassung konnte nicht geladen werden.</p>'

function readConfig(): PrepareConfig {
  const config = readJsonScript<PrepareConfig>(CONFIG_ID)
  if (!config) throw new Error(`Vorbereitungs-Konfiguration #${CONFIG_ID} fehlt`)
  return config
}

/**
 * Objekt von Alpines Reaktivität ausnehmen (entspricht Vues `markRaw`, auf dem Alpine aufbaut).
 *
 * TipTap-/ProseMirror-Instanzen dürfen nicht in einen reaktiven Proxy gewickelt werden:
 * ProseMirror prüft beim Anwenden einer Transaktion, ob sie auf *demselben* Dokument-Objekt
 * aufbaut. Über den Proxy entstehen Transaktionen auf Proxy-Objekten, und `setContent` bzw.
 * die Toolbar-Befehle scheitern mit „Applying a mismatched transaction".
 */
function markRaw<T extends object>(value: T): T {
  Object.defineProperty(value, '__v_skip', { value: true, configurable: true })
  return value
}

/** Alpine-Komponente: `x-data="preparationApp"` */
export const preparationApp = defineComponent(() => {
  const config = readConfig()
  const base = `/work/${config.orgSlug}/meetings`

  return {
    // ---------- Daten ----------
    items: config.items,
    search: '',
    selectedItemId: null as string | null,
    selectedItem: null as PreparedItem | null,
    mobileTab: 'tops',

    // Konstanten
    orgSlug: config.orgSlug,
    meetingId: config.meetingId,
    currentUser: config.currentUser,
    positionLabels: config.positionLabels,

    // Auto-Save-Status
    pendingSaves: 0,
    lastSavedAt: null as string | null,
    saveError: false,
    _timers: {} as Record<string, number>,
    _pending: {} as Record<string, () => void>,

    // Org-Sitzungsnotizen
    orgNotesOpen: false,
    orgNotes: config.orgNotes,

    // Diskussions-Thread
    thread: [] as ThreadNote[],
    threadLoading: false,
    newComment: '',
    commentVisibility: 'organization',
    commentIsPosition: false,
    realtime: 'off' as RealtimeMode,
    _sockets: [] as WebSocket[],
    _pollTimer: null as number | null,
    _wsWatchdog: null as number | null,

    // Redebeitrag
    speechOpen: false,
    // Die Templates greifen direkt auf `speechEditor` zu (Toolbar-Buttons)
    speechEditor: null as Editor | null,
    speechFormats: {} as Partial<FormatState>,
    speechDurationText: '',
    speechReadonly: false,
    _suppressEditorSave: false,
    _editorItem: null as PreparedItem | null,

    // Anhänge
    docs: [] as SupplementaryDoc[],
    docsLoading: false,
    newLinkTitle: '',
    newLinkUrl: '',
    attachToPaper: false,
    uploading: false,

    // PDF-Vorschau (Muster RIS-Dokumentvorschau) + Anmerkungen
    preview: {
      open: false,
      kind: null,
      id: null,
      title: '',
      baseSrc: '',
      frameSrc: '',
      externalUrl: '',
      source: null,
    } as PreviewState,
    previewLoading: false,
    annotations: [] as Annotation[],
    annotationsLoading: false,
    newAnnotationText: '',
    newAnnotationPage: 1 as number | string,

    // Modals
    showLegend: false,
    showSummary: false,
    summaryHtml: '',
    summaryLoading: false,
    docModalOpen: false,
    docQuery: '',
    docResults: [] as Array<Record<string, unknown> & { id: string }>,
    docSearching: false,

    // ---------- Computed ----------
    get filteredItems(): PreparedItem[] {
      const q = this.search.trim().toLowerCase()
      if (!q) return this.items
      return this.items.filter(
        (i) => (i.name || '').toLowerCase().includes(q) || String(i.number).toLowerCase().includes(q),
      )
    },
    get positionedCount(): number {
      return this.items.filter((i) => i.position && i.position !== 'open').length
    },
    get saveStatusText(): string {
      if (this.pendingSaves > 0) return 'Speichert…'
      if (this.saveError) return 'Speichern fehlgeschlagen — Änderungen erneut vornehmen'
      if (this.lastSavedAt) return 'Gespeichert ' + this.lastSavedAt
      return 'Änderungen werden automatisch gespeichert'
    },
    get annotationGroups(): Array<{ page: number; entries: Annotation[] }> {
      const byPage: Record<number, Annotation[]> = {}
      for (const a of this.annotations) {
        if (!byPage[a.page]) byPage[a.page] = []
        byPage[a.page].push(a)
      }
      return Object.keys(byPage)
        .map(Number)
        .sort((x, y) => x - y)
        .map((page) => ({ page, entries: byPage[page] }))
    },

    // ---------- Init ----------
    init(): void {
      if (this.items.length > 0) {
        this.selectItem(this.items[0].id)
        if (window.innerWidth >= 1280) this.mobileTab = 'main'
      }
      window.addEventListener('beforeunload', () => this.teardownRealtime())
    },

    // ---------- TOP-Auswahl & Navigation ----------
    selectItem(itemId: string): void {
      if (this.selectedItemId === itemId) return
      this.flushTimers()
      this.closePreview()
      this.selectedItemId = itemId
      this.selectedItem = this.items.find((i) => i.id === itemId) || null
      if (!this.selectedItem) return

      const item = this.selectedItem
      this.speechOpen = !!(item.hasSpeechNote || item.speechLinkedDocument)
      this.speechDurationText = this.formatDuration(item.speechDuration || 0)
      this.speechReadonly = !!item.speechLinkedDocument
      this.attachToPaper = false
      this.thread = []
      this.docs = []

      void this.loadThread()
      void this.loadDocs()
      this.connectRealtime()
      void this.syncSpeechEditor()
      void this.$nextTick(() => {
        const el = document.getElementById('nav-item-' + itemId)
        if (el) el.scrollIntoView({ block: 'nearest' })
      })
    },

    selectByOffset(offset: number): void {
      const list = this.filteredItems
      if (!list.length) return
      const idx = list.findIndex((i) => i.id === this.selectedItemId)
      const next = list[Math.min(list.length - 1, Math.max(0, idx + offset))]
      if (next && next.id !== this.selectedItemId) this.selectItem(next.id)
    },

    handleKeydown(e: KeyboardEvent): void {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
      if (this.showLegend || this.showSummary || this.docModalOpen) return
      const t = e.target as HTMLElement | null
      const tag = (t?.tagName || '').toLowerCase()
      if (['input', 'textarea', 'select'].includes(tag) || t?.isContentEditable) return
      e.preventDefault()
      this.selectByOffset(e.key === 'ArrowDown' ? 1 : -1)
    },

    // ---------- Auto-Save-Infrastruktur ----------
    queue(key: string, fn: () => void, delay = 800): void {
      window.clearTimeout(this._timers[key])
      this._pending[key] = fn
      this._timers[key] = window.setTimeout(() => {
        delete this._timers[key]
        delete this._pending[key]
        fn()
      }, delay)
    },

    // Debounce pro TOP: die Item-Referenz wird beim Tippen eingefangen,
    // damit ein TOP-Wechsel während der Wartezeit nichts Falsches speichert.
    queueItemSave(prefix: string, item: PreparedItem, fn: (item: PreparedItem) => void, delay = 800): void {
      this.queue(prefix + '-' + item.id, () => fn(item), delay)
    },

    // Anstehende Debounce-Saves beim TOP-Wechsel NICHT verwerfen, sondern
    // sofort ausfuehren — die Callbacks halten ihre TOP-Referenz, sodass die
    // Eingabe am korrekten (alten) TOP gespeichert wird.
    flushTimers(): void {
      for (const key of Object.keys(this._timers)) {
        window.clearTimeout(this._timers[key])
        delete this._timers[key]
      }
      const pending = this._pending
      this._pending = {}
      for (const key of Object.keys(pending)) {
        try {
          pending[key]()
        } catch (e) {
          console.error('Flush-Save fehlgeschlagen:', key, e)
        }
      }
    },

    async apiSave(url: string, body?: unknown, method = 'POST'): Promise<JsonResponse | null> {
      this.pendingSaves++
      try {
        const headers: Record<string, string> = { 'X-CSRFToken': csrfToken() }
        const opts: RequestInit = { method, headers }
        if (body instanceof FormData) {
          opts.body = body
        } else if (body !== undefined) {
          headers['Content-Type'] = 'application/json'
          opts.body = JSON.stringify(body)
        }
        const resp = await fetch(url, opts)
        if (!resp.ok) throw new Error('HTTP ' + resp.status)
        this.saveError = false
        this.lastSavedAt = new Date().toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
        return (await resp.json()) as JsonResponse
      } catch (err) {
        console.error('Speichern fehlgeschlagen:', url, err)
        this.saveError = true
        return null
      } finally {
        this.pendingSaves--
      }
    },

    // ---------- Position & Ergebnis ----------
    posLabel(code: string): string {
      return this.positionLabels[code] || code
    },
    posDot(code: string): string {
      return POSITION_DOTS[code] || 'bg-gray-400'
    },
    posChip(code: string): string {
      return POSITION_CHIPS[code] || 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
    },

    setPosition(code: string): void {
      if (!this.selectedItem) return
      this.selectedItem.position = code
      void this.savePositionFields(this.selectedItem, { position: code })
    },
    saveOutcome(): void {
      if (!this.selectedItem) return
      void this.savePositionFields(this.selectedItem, { outcome: this.selectedItem.outcome })
    },
    async savePositionFields(item: PreparedItem, fields: Record<string, unknown>): Promise<void> {
      await this.apiSave(`${base}/${this.meetingId}/position/${item.id}/`, fields)
    },

    // ---------- Private Notiz ----------
    async savePrivateNote(item: PreparedItem): Promise<void> {
      await this.apiSave(`${base}/${this.meetingId}/private-note/${item.id}/`, {
        content: item.privateNote || '',
      })
    },

    // ---------- Org-Sitzungsnotizen ----------
    async saveOrgNotes(): Promise<void> {
      await this.apiSave(`${base}/${this.meetingId}/prepare/`, { notes: this.orgNotes || '' })
    },

    // ---------- Redebeitrag ----------
    speechUrl(item: PreparedItem): string {
      return `${base}/${this.meetingId}/speech/${item.id}/`
    },
    teleprompterUrl(): string {
      return `${base}/${this.meetingId}/teleprompter/${this.selectedItemId}/`
    },

    openSpeech(): void {
      this.speechOpen = true
      void this.syncSpeechEditor(true)
    },

    ensureSpeechEditor(): void {
      const MandariEditor = window.MandariEditor
      if (this.speechEditor || !MandariEditor) return
      const el = this.$refs.speechEditorEl
      if (!el) return
      // Nicht reaktiv ablegen, siehe markRaw()
      this.speechEditor = markRaw(
        MandariEditor.createEditor({
          element: el,
          content: '',
          editable: true,
          placeholder: 'Redetext verfassen — mit / öffnen Sie das Einfüge-Menü...',
          onUpdate: (html) => {
            if (this._suppressEditorSave) return
            const item = this._editorItem
            if (!item || this.speechReadonly) return
            item.speechContent = html
            item.hasSpeechNote = true
            this.queueItemSave('speech-content', item, (it) => {
              void this.saveSpeechFields(it, { content: it.speechContent })
            })
          },
          onSelectionUpdate: (state) => {
            this.speechFormats = state
          },
        }),
      )
    },

    // Editorinhalt auf den aktuellen TOP setzen (bei verknüpftem Dokument
    // wird der Inhalt frisch von der API geholt, da er read-only aus dem
    // Dokument kommt).
    async syncSpeechEditor(focus = false): Promise<void> {
      await this.$nextTick()
      this.ensureSpeechEditor()
      if (!this.speechEditor) return
      const item = this.selectedItem
      if (!item) return
      this._editorItem = item
      let content = item.speechContent || ''
      if (item.speechLinkedDocument) {
        const data = await this.fetchJson(this.speechUrl(item))
        // Stale-Response-Schutz: wurde inzwischen der TOP gewechselt,
        // darf diese Antwort den Editor des neuen TOP nicht ueberschreiben.
        if (this.selectedItemId !== item.id) return
        if (data?.own) content = data.own.content || ''
      }
      this._suppressEditorSave = true
      this.speechEditor.commands.setContent(content)
      this._suppressEditorSave = false
      this.speechEditor.setEditable(!this.speechReadonly)
      if (focus && !this.speechReadonly) this.speechEditor.commands.focus()
    },

    async saveSpeechFields(item: PreparedItem, fields: Record<string, unknown>): Promise<void> {
      const data = await this.apiSave(this.speechUrl(item), fields)
      if (data?.success) {
        item.hasSpeechNote = true
        if (data.speech) {
          item.speechShared = data.speech.is_shared
          item.speechLinkedDocument = data.speech.linked_document
        }
      }
    },

    saveSpeechDuration(): void {
      const item = this.selectedItem
      if (!item) return
      const seconds = this.parseDuration(this.speechDurationText)
      item.speechDuration = seconds
      this.speechDurationText = this.formatDuration(seconds)
      void this.saveSpeechFields(item, { estimated_duration: seconds })
    },

    async deleteSpeech(): Promise<void> {
      if (
        !(await confirmAction({
          title: 'Redebeitrag löschen',
          message: 'Möchten Sie den Redebeitrag zu diesem TOP wirklich löschen?',
          confirmText: 'Löschen',
          variant: 'danger',
        }))
      )
        return
      const item = this.selectedItem
      if (!item) return
      const data = await this.apiSave(this.speechUrl(item), undefined, 'DELETE')
      if (data?.success) {
        item.speechTitle = ''
        item.speechContent = ''
        item.speechDuration = 0
        item.speechShared = false
        item.hasSpeechNote = false
        item.speechLinkedDocument = null
        this.speechReadonly = false
        this.speechDurationText = this.formatDuration(0)
        if (this.speechEditor) {
          this._suppressEditorSave = true
          this.speechEditor.commands.setContent('')
          this._suppressEditorSave = false
          this.speechEditor.setEditable(true)
        }
      }
    },

    // Dokument-Verknüpfung
    async searchDocs(): Promise<void> {
      this.docSearching = true
      try {
        const data = await this.fetchJson(`${base}/speech-documents/?q=${encodeURIComponent(this.docQuery)}`)
        this.docResults = data?.documents || []
      } finally {
        this.docSearching = false
      }
    },

    async linkDocument(doc: { id: string }): Promise<void> {
      const item = this.selectedItem
      if (!item) return
      const data = await this.apiSave(this.speechUrl(item), { linked_document: doc.id })
      if (data?.success) {
        this.docModalOpen = false
        item.speechLinkedDocument = data.speech.linked_document
        item.hasSpeechNote = true
        this.speechReadonly = true
        this.applyEditorContent(data.speech.content || '')
      }
    },

    async unlinkDocument(): Promise<void> {
      const item = this.selectedItem
      if (!item) return
      const data = await this.apiSave(this.speechUrl(item), { linked_document: null })
      if (data?.success) {
        item.speechLinkedDocument = null
        this.speechReadonly = false
        this.applyEditorContent(data.speech.content || '')
      }
    },

    applyEditorContent(html: string): void {
      this.ensureSpeechEditor()
      if (!this.speechEditor) return
      this._suppressEditorSave = true
      this.speechEditor.commands.setContent(html)
      this._suppressEditorSave = false
      this.speechEditor.setEditable(!this.speechReadonly)
      if (this._editorItem) {
        this._editorItem.speechContent = this.speechReadonly ? this._editorItem.speechContent : html
      }
    },

    // ---------- Diskussion (einheitlicher Thread) ----------
    notesUrl(item: PreparedItem): string {
      return `${base}/${this.meetingId}/notes/${item.id}/`
    },

    async loadThread(silent = false): Promise<void> {
      const item = this.selectedItem
      if (!item) return
      if (!silent) this.threadLoading = true
      try {
        const data = await this.fetchJson(this.notesUrl(item))
        if (data && this.selectedItemId === item.id) {
          this.thread = data.notes || []
          item.notesCount = this.thread.length
        }
      } finally {
        this.threadLoading = false
      }
    },

    async postComment(): Promise<void> {
      const item = this.selectedItem
      if (!item || !this.newComment.trim()) return
      const data = await this.apiSave(this.notesUrl(item), {
        content: this.newComment.trim(),
        visibility: this.commentVisibility,
        is_decision: this.commentIsPosition,
      })
      if (data?.success) {
        this.newComment = ''
        this.commentIsPosition = false
        this.addThreadEntry(data.note, item)
      }
    },

    async deleteComment(noteId: string): Promise<void> {
      if (
        !(await confirmAction({
          title: 'Beitrag löschen',
          message: 'Möchten Sie diesen Beitrag wirklich löschen?',
          confirmText: 'Löschen',
          variant: 'danger',
        }))
      )
        return
      const item = this.selectedItem
      const data = await this.apiSave(`${base}/notes/${noteId}/delete/`, undefined, 'DELETE')
      if (data?.success) {
        this.thread = this.thread.filter((n) => n.id !== noteId)
        if (item) item.notesCount = this.thread.length
      }
    },

    addThreadEntry(note: ThreadNote | null | undefined, item: PreparedItem | null): void {
      if (!note || this.thread.some((n) => n.id === note.id)) return
      this.thread.unshift(note)
      if (item) item.notesCount = this.thread.length
    },

    // ---------- Echtzeit: WebSocket + Polling-Fallback ----------
    connectRealtime(): void {
      this.teardownRealtime()
      const item = this.selectedItem
      if (!item || typeof WebSocket === 'undefined') {
        this.startPolling()
        return
      }

      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const wsBase = `${proto}://${window.location.host}/ws/preparation/${this.orgSlug}`
      const urls = [`${wsBase}/item/${item.id}/`]
      if (item.paper) urls.push(`${wsBase}/paper/${item.paper.id}/`)

      let openCount = 0
      for (const url of urls) {
        try {
          const ws = new WebSocket(url)
          ws.onopen = () => {
            openCount++
            this.realtime = 'ws'
            this.stopPolling()
          }
          ws.onmessage = (ev) => {
            try {
              this.handleRealtimeEvent(JSON.parse(ev.data) as RealtimeMessage, item)
            } catch {
              /* ignorieren */
            }
          }
          ws.onclose = () => {
            openCount = Math.max(0, openCount - 1)
            if (openCount === 0 && this.selectedItemId === item.id) {
              this.realtime = 'off'
              this.startPolling()
            }
          }
          this._sockets.push(ws)
        } catch (e) {
          console.warn('WebSocket nicht verfügbar:', e)
        }
      }
      // Wächter: wenn nach 4s keine Verbindung steht, aufs Polling ausweichen
      this._wsWatchdog = window.setTimeout(() => {
        if (this.realtime !== 'ws' && this.selectedItemId === item.id) this.startPolling()
      }, 4000)
    },

    teardownRealtime(): void {
      if (this._wsWatchdog) {
        window.clearTimeout(this._wsWatchdog)
        this._wsWatchdog = null
      }
      for (const ws of this._sockets) {
        ws.onclose = null
        try {
          ws.close()
        } catch {
          /* ignorieren */
        }
      }
      this._sockets = []
      this.realtime = 'off'
      this.stopPolling()
    },

    startPolling(): void {
      if (this._pollTimer) return
      this.realtime = 'poll'
      this._pollTimer = window.setInterval(() => {
        if (this.selectedItemId) void this.loadThread(true)
      }, 5000)
    },

    stopPolling(): void {
      if (this._pollTimer) {
        window.clearInterval(this._pollTimer)
        this._pollTimer = null
      }
    },

    handleRealtimeEvent(msg: RealtimeMessage, item: PreparedItem): void {
      if (!msg || this.selectedItemId !== item.id) return

      if (msg.type === 'comment' && msg.event === 'created' && msg.comment) {
        this.addThreadEntry(msg.comment, item)
      } else if (msg.type === 'comment' && msg.event === 'deleted' && msg.comment_id) {
        this.thread = this.thread.filter((n) => n.id !== msg.comment_id)
        item.notesCount = this.thread.length
      } else if (msg.type === 'position' && msg.event === 'updated') {
        if (msg.agenda_item_id === item.id && msg.position) {
          // Eigene TOP-Position von anderem Org-Mitglied aktualisiert
          const p = msg.position
          if (p.position) item.position = p.position
          if ('is_final' in p) item.isFinal = !!p.is_final
          if ('outcome' in p) item.outcome = p.outcome || ''
          // Begründung nicht überschreiben, während hier getippt wird
          const active = document.activeElement
          if ('reasoning' in p && (!active || active.id !== 'reasoning-input')) {
            item.reasoning = p.reasoning || ''
          }
        } else {
          // Position aus anderem Gremium zur selben Vorlage: Verlauf auffrischen
          void this.refreshCrossPositions(item)
        }
      }
    },

    async refreshCrossPositions(item: PreparedItem): Promise<void> {
      const data = await this.fetchJson(`${base}/${this.meetingId}/position/${item.id}/`)
      if (data?.cross_positions && this.selectedItemId === item.id) {
        item.crossPositions = data.cross_positions
      }
    },

    // ---------- PDF-Vorschau + Anmerkungen ----------
    openPreview(
      kind: string | undefined,
      id: string | undefined,
      title: string | undefined,
      src: string | undefined,
      externalUrl?: string,
      source?: PreviewSource | null,
    ): void {
      if (!kind || !id || !src) return
      this.preview = {
        open: true,
        kind,
        id,
        title: title || 'Dokument',
        baseSrc: src,
        frameSrc: src,
        externalUrl: externalUrl || src,
        source: source || null,
      }
      this.previewLoading = true
      this.annotations = []
      this.newAnnotationText = ''
      this.newAnnotationPage = 1
      void this.loadAnnotations()
    },

    previewDoc(doc: SupplementaryDoc): void {
      this.openPreview(doc.preview_kind, doc.preview_id, doc.title, doc.preview_url, doc.url, doc)
    },

    closePreview(): void {
      this.preview.open = false
      this.preview.frameSrc = ''
      this.previewLoading = false
    },

    // Seitensprung über den PDF-Anker #page=N (funktioniert in den
    // eingebetteten Viewern von Chrome/Firefox; das PDF kommt aus dem Cache)
    previewGotoPage(page: number | string): void {
      const p = Math.max(1, Number.parseInt(String(page), 10) || 1)
      this.newAnnotationPage = p
      this.previewLoading = true
      this.preview.frameSrc = this.preview.baseSrc.split('#')[0] + '#page=' + p
    },

    annotationsUrl(): string {
      return `${base}/annotations/${this.preview.kind}/${this.preview.id}/`
    },

    async loadAnnotations(): Promise<void> {
      const kind = this.preview.kind
      const id = this.preview.id
      this.annotationsLoading = true
      try {
        const data = await this.fetchJson(this.annotationsUrl())
        if (data && this.preview.kind === kind && this.preview.id === id) {
          this.annotations = data.annotations || []
          this.syncAnnotationCount(data.count)
        }
      } finally {
        this.annotationsLoading = false
      }
    },

    // Zähler-Badge an der Datei (RIS-Karte bzw. eigene Anlage) aktuell halten
    syncAnnotationCount(count: unknown): void {
      if (typeof count !== 'number' || !this.preview.source) return
      this.preview.source.annotations = count
    },

    async addAnnotation(): Promise<void> {
      if (!this.preview.open || !this.newAnnotationText.trim()) return
      const data = await this.apiSave(this.annotationsUrl(), {
        page: Math.max(1, Number.parseInt(String(this.newAnnotationPage), 10) || 1),
        content: this.newAnnotationText.trim(),
      })
      if (data?.success) {
        this.newAnnotationText = ''
        this.annotations.push(data.annotation)
        this.annotations.sort((a, b) => a.page - b.page || (a.created_at < b.created_at ? -1 : 1))
        this.syncAnnotationCount(data.count)
      }
    },

    async deleteAnnotation(a: Annotation): Promise<void> {
      if (
        !(await confirmAction({
          title: 'Anmerkung löschen',
          message: 'Möchten Sie diese Anmerkung wirklich löschen?',
          confirmText: 'Löschen',
          variant: 'danger',
        }))
      )
        return
      const data = await this.apiSave(`${base}/annotations/${a.id}/delete/`, undefined, 'DELETE')
      if (data?.success) {
        this.annotations = this.annotations.filter((x) => x.id !== a.id)
        this.syncAnnotationCount(this.annotations.length)
      }
    },

    // ---------- Anhänge ----------
    supplementaryUrl(item: PreparedItem): string {
      return `${base}/${this.meetingId}/supplementary/${item.id}/`
    },

    async loadDocs(): Promise<void> {
      const item = this.selectedItem
      if (!item) return
      this.docsLoading = true
      try {
        const data = await this.fetchJson(this.supplementaryUrl(item))
        if (data && this.selectedItemId === item.id) {
          this.docs = data.documents || []
        }
      } finally {
        this.docsLoading = false
      }
    },

    _paperAnchorPayload(): { paper_id?: string; share_across_committees?: boolean } {
      const item = this.selectedItem
      if (this.attachToPaper && item?.paper) {
        return { paper_id: item.paper.id, share_across_committees: true }
      }
      return {}
    },

    async addLink(): Promise<void> {
      const item = this.selectedItem
      if (!item || !this.newLinkTitle.trim() || !this.newLinkUrl.trim()) return
      const data = await this.apiSave(this.supplementaryUrl(item), {
        document_type: 'link',
        title: this.newLinkTitle.trim(),
        url: this.newLinkUrl.trim(),
        ...this._paperAnchorPayload(),
      })
      if (data?.success) {
        this.newLinkTitle = ''
        this.newLinkUrl = ''
        this.docs.push({
          ...data.document,
          document_type: data.document.document_type || 'link',
          added_by: this.currentUser,
          is_from_other_item: false,
        })
      }
    },

    async uploadFile(event: Event): Promise<void> {
      const item = this.selectedItem
      const input = event.target as HTMLInputElement
      const file = input.files?.[0]
      input.value = ''
      if (!item || !file) return
      this.uploading = true
      try {
        const form = new FormData()
        form.append('file', file)
        form.append('title', file.name)
        const anchor = this._paperAnchorPayload()
        if (anchor.paper_id) {
          form.append('paper_id', anchor.paper_id)
          form.append('share_across_committees', 'true')
        }
        const data = await this.apiSave(this.supplementaryUrl(item), form)
        if (data?.success) {
          this.docs.push({
            ...data.document,
            added_by: this.currentUser,
            is_from_other_item: false,
          })
        }
      } finally {
        this.uploading = false
      }
    },

    async deleteDoc(doc: SupplementaryDoc): Promise<void> {
      if (
        !(await confirmAction({
          title: 'Anlage entfernen',
          message: `„${doc.title}" wirklich entfernen?`,
          confirmText: 'Entfernen',
          variant: 'danger',
        }))
      )
        return
      const data = await this.apiSave(`${base}/supplementary/${doc.id}/delete/`, undefined, 'DELETE')
      if (data?.success) {
        this.docs = this.docs.filter((d) => d.id !== doc.id)
      }
    },

    // ---------- Zusammenfassung ----------
    async openSummary(): Promise<void> {
      this.showSummary = true
      this.summaryLoading = true
      try {
        const resp = await fetch(config.urls.summary)
        this.summaryHtml = resp.ok ? await resp.text() : SUMMARY_ERROR
      } catch {
        this.summaryHtml = SUMMARY_ERROR
      } finally {
        this.summaryLoading = false
      }
    },

    // ---------- Hilfsfunktionen ----------
    async fetchJson(url: string): Promise<JsonResponse | null> {
      try {
        const resp = await fetch(url)
        if (!resp.ok) return null
        return (await resp.json()) as JsonResponse
      } catch {
        return null
      }
    },

    autoGrow(el: HTMLElement, maxRem = 20): void {
      el.style.height = 'auto'
      const max = maxRem * 16
      el.style.height = Math.min(max, el.scrollHeight) + 'px'
      el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden'
    },

    stripHtml(html: string | null | undefined): string {
      const div = document.createElement('div')
      div.innerHTML = html || ''
      return div.textContent || ''
    },

    fmtDateTime(iso: string | null | undefined): string {
      if (!iso) return ''
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return ''
      return (
        d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
        ' ' +
        d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
      )
    },

    formatDuration(seconds: number | string | null | undefined): string {
      const s = Math.max(0, Number.parseInt(String(seconds || 0), 10))
      const m = Math.floor(s / 60)
      return m + ':' + String(s % 60).padStart(2, '0')
    },

    parseDuration(text: string | number | null | undefined): number {
      const t = String(text || '').trim()
      if (!t) return 0
      if (t.includes(':')) {
        const [m, s] = t.split(':')
        return Math.max(0, (Number.parseInt(m, 10) || 0) * 60 + (Number.parseInt(s, 10) || 0))
      }
      return Math.max(0, Number.parseInt(t, 10) || 0)
    },
  }
})
