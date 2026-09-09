/**
 * Dokumenteditor (Alpine-Komponente `documentEditor`).
 *
 * TipTap über `window.MandariEditor`, Kollaboration (Yjs) mit Solo-Fallback, Autosave,
 * Kommentare (Sidebar + Inline-Marks), Suchen & Ersetzen, Links, Bilder, Briefkopf,
 * KI-Panel und Versionsverlauf. Die Konfiguration kommt aus dem View
 * (`editor_config`) per `{{ editor_config|json_script:"document-editor-config" }}`.
 *
 * Markup: `templates/work/motions/editor.html` und die `_editor_*`-Partials.
 */

import type { Editor } from '@tiptap/core'
import type { CollabUser } from '../editor/collaboration'
import type { FormatState } from '../editor/index'
import { defineComponent } from '../js/alpine/component'
import { confirmAction } from '../js/alpine/confirm-dialog'
import { showToast } from '../js/alpine/toast'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'
import { installMotionTracking } from '../js/motion-tracking'

// ---- Konfiguration aus dem View -------------------------------------------------

export interface LetterheadMargins {
  top: number
  right: number
  bottom: number
  left: number
}

/** Aktuell zugewiesener Briefkopf (Hintergrund beim Laden) */
export interface ActiveLetterheadConfig {
  kind: 'generated' | 'pdf'
  previewUrl: string
  pdfUrl: string
  margins: LetterheadMargins
}

/** Eintrag der Briefkopf-Auswahl (Dropdown) */
export interface LetterheadOption {
  id: string
  name: string
  kind: string
  pdf_url: string
  preview_url: string
  margin_top: number
  margin_right: number
  margin_bottom: number
  margin_left: number
  font_family: string
  font_size: number
}

export interface InlineCommentReply {
  id: string
  content: string
  author_name: string
  author_initials: string
  created_at: string
}

export interface InlineComment {
  id: string
  mark_id: string
  content: string
  selected_text: string
  author_name: string
  author_initials: string
  created_at: string
  is_resolved: boolean
  replies: InlineCommentReply[]
}

export interface DocumentEditorConfig {
  motionId: string
  title: string
  visibility: string
  motionType: string
  documentTypeId: string
  documentTypeName: string
  letterheadId: string
  letterheadName: string
  accessLevel: string
  aiQuotaLimit: number | null
  aiQuotaUsed: number
  collabUserName: string
  collabUserColor: string
  letterhead: ActiveLetterheadConfig | null
  letterheads: LetterheadOption[]
  inlineComments: InlineComment[]
  urls: {
    /** POST: Kommentar anlegen; `<id>/resolve/` darunter: erledigen */
    comment: string
    /** POST: Statuswechsel */
    status: string
    /** Dokumentliste (Ziel nach dem Löschen) */
    documents: string
    /** POST: KI-Aktionen */
    ai: string
    /** GET: Versionsliste; `<id>/` und `<id>/restore/` darunter */
    revisions: string
    /** POST: Checkliste (Tracking-Sidebar) */
    checklist: string
  }
}

// ---- Zustandstypen ----------------------------------------------------------------

export interface ChatMessage {
  role: 'user' | 'ai'
  content: string
  hasAction?: boolean
  actionContent?: string | null
}

export interface Revision {
  id: string
  version: number
  [key: string]: unknown
}

type CollabStatus = 'connecting' | 'connected' | 'disconnected'

// biome-ignore lint/suspicious/noExplicitAny: Antworten der JSON-Endpunkte sind nicht schematisiert
type JsonResponse = Record<string, any>

const CONFIG_ID = 'document-editor-config'
const PLACEHOLDER = 'Beginnen Sie hier mit dem Schreiben...'

function readConfig(): DocumentEditorConfig {
  const config = readJsonScript<DocumentEditorConfig>(CONFIG_ID)
  if (!config) throw new Error(`Editor-Konfiguration #${CONFIG_ID} fehlt`)
  return config
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function formHeaders(): Record<string, string> {
  return {
    'Content-Type': 'application/x-www-form-urlencoded',
    'X-Requested-With': 'XMLHttpRequest',
    'X-CSRFToken': csrfToken(),
  }
}

/** Alpine-Komponente: `x-data="documentEditor"` */
export const documentEditor = defineComponent(() => {
  const config = readConfig()

  // WICHTIG: Der TipTap-Editor liegt außerhalb des reaktiven Alpine-Zustands.
  // Alpine verpackt Komponentenfelder in Proxies; das bricht die
  // Identitätsprüfungen von ProseMirror ("Applying a mismatched transaction").
  let editor: Editor | null = null
  let autoSaveInterval: number | null = null

  return {
    updatedAt: Date.now(),
    title: config.title,
    wordCount: 0,
    charCount: 0,
    pageCount: 1,
    currentPage: 1,
    lastSaved: null as string | null,
    saveError: false,
    saving: false,
    // Suchen & Ersetzen
    searchOpen: false,
    searchTerm: '',
    replaceTerm: '',
    searchMatchCase: false,
    searchCurrent: 0,
    searchTotal: 0,
    // Link-Dialog & -Popover
    showLinkModal: false,
    linkUrl: '',
    linkPopupVisible: false,
    linkPopupHref: '',
    linkPopupTop: 0,
    linkPopupLeft: 0,
    _globalKeydownHandler: null as ((e: KeyboardEvent) => void) | null,
    showMobileSidebar: false,
    showShareModal: false,
    shareMotionId: config.motionId,
    shareMotionTitle: config.title,
    shareVisibility: config.visibility,
    addUserEmail: '',
    sidebarTab: 'comments',
    aiLoading: false,
    aiQuotaLimit: config.aiQuotaLimit,
    aiQuotaUsed: config.aiQuotaUsed,
    chatMessages: [] as ChatMessage[],
    userMessage: '',
    aiPreviewActive: false,
    aiPreviewContent: '',
    aiOriginalContent: '',
    previewMode: 'suggested',
    // Kommentar-Sidebar
    showResolvedComments: false,
    replyingToComment: null as string | null,
    newCommentContent: '',
    commentSubmitting: false,
    selectedText: '',
    selectionStart: 0,
    selectionEnd: 0,
    // Aktiver Kommentar (Sidebar <-> Editor-Mark in beide Richtungen)
    activeCommentId: null as string | null,
    // Popup zum Anlegen eines Inline-Kommentars (bei Textauswahl)
    commentPopupVisible: false,
    commentPopupExpanded: false,
    commentPopupTop: 0,
    commentPopupLeft: 0,
    inlineCommentText: '',
    inlineSelectedText: '',
    _selectionFrom: 0,
    _selectionTo: 0,
    // Popup mit Kommentardetails (Klick auf bestehende Markierung)
    markCommentPopup: false,
    markCommentData: null as InlineComment | null,
    markCommentTop: 0,
    markCommentLeft: 0,
    markReplyText: '',
    markReplySubmitting: false,
    inlineCommentsData: config.inlineComments,
    // Kollaboration
    collabEnabled: false,
    collabStatus: 'disconnected' as CollabStatus,
    collabUsers: [] as CollabUser[],
    collabUserName: config.collabUserName,
    collabUserColor: config.collabUserColor,
    collabDestroy: null as (() => void) | null,
    _everConnected: false,
    // Versionsverlauf
    historyMode: false,
    revisions: [] as Revision[],
    revisionsLoading: false,
    selectedRevision: null as Revision | null,
    revisionContent: '',
    revisionDiff: '',
    revisionLoading: false,
    revisionViewMode: 'diff',
    // Bild einfügen
    showImageModal: false,
    imageUrl: '',
    imageAlt: '',
    formats: {
      bold: false,
      italic: false,
      underline: false,
      strike: false,
      header: false,
      list: false,
      blockquote: false,
      link: false,
      textAlign: 'left',
      textColor: false,
      highlight: false,
      table: false,
    } as FormatState,
    motionId: config.motionId,
    motionType: config.motionType,
    documentTypeId: config.documentTypeId,
    documentTypeName: config.documentTypeName,
    letterheadId: config.letterheadId,
    letterheadName: config.letterheadName,
    letterheadsData: config.letterheads,
    accessLevel: config.accessLevel,
    _editorInitialized: false,
    _collabFailed: false,
    _recoveringEditor: false,

    init(): void {
      if (this._editorInitialized) return
      this._editorInitialized = true
      installMotionTracking(config.urls.checklist)

      const canEdit = this.accessLevel === 'edit' || this.accessLevel === 'admin'
      const MandariEditor = window.MandariEditor

      if (canEdit && MandariEditor) {
        // Initialer Inhalt aus dem versteckten Template
        const tpl = document.getElementById('editor-initial-content')
        const initialContent = tpl ? tpl.innerHTML : ''
        const editorEl = document.getElementById('editor-container') as HTMLElement

        // WebSocket-URL für die Kollaboration
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const wsUrl = `${wsProtocol}//${window.location.host}/ws/documents/${this.motionId}/`

        // Kollaboration ist Standard für alle Bearbeiter (Google-Docs-Modus).
        // Schlägt die erste WebSocket-Verbindung fehl, wechselt der Editor
        // automatisch in den Solo-Modus (_switchToSoloMode).
        if (typeof MandariEditor.createCollaborativeEditor === 'function') {
          this.collabEnabled = true
          this._initCollabEditor(editorEl, initialContent, wsUrl)
        } else {
          this.collabEnabled = false
          this._initSoloEditor(editorEl, initialContent)
        }
        this._bindToolbarCommands()
        this._bindExtendedToolbar()

        // Globale Tastenkürzel: Strg+S = Speichern, Strg+F = Suchen & Ersetzen
        this._globalKeydownHandler = (e: KeyboardEvent) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault()
            void this.save()
          } else if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'f') {
            e.preventDefault()
            this.openSearch()
          } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            // Strg+K = Link einfügen (nur wenn der Editor den Fokus hat)
            if (editor?.isFocused) {
              e.preventDefault()
              this.openLinkDialog()
            }
          } else if (e.key === 'Escape' && this.searchOpen) {
            this.closeSearch()
          }
        }
        document.addEventListener('keydown', this._globalKeydownHandler)

        this.updateWordCount()

        // Autosave alle 60 Sekunden
        autoSaveInterval = window.setInterval(() => this.autoSave(), 60000)
      }

      // Briefkopf-Hintergrund rendern, falls vorhanden
      const letterhead = config.letterhead
      if (letterhead) {
        if (letterhead.kind === 'generated') {
          // Generierter Briefkopf: HTML-Vorschau über dem Inhalt (statt pdfjs-Overlay)
          void this._renderGeneratedLetterhead(letterhead.previewUrl, letterhead.margins)
        } else if (MandariEditor?.renderLetterhead) {
          void MandariEditor.renderLetterhead({
            pdfUrl: letterhead.pdfUrl,
            container: document.getElementById('editor-container') as HTMLElement,
            opacity: 0.25,
            margins: letterhead.margins,
          })
        }
      }

      // Klick-Handler für bestehende Inline-Kommentar-Markierungen und Links
      const editorClickEl = document.getElementById('editor-container')
      if (editorClickEl) {
        editorClickEl.addEventListener('click', (e) => {
          const target = e.target as Element
          const mark = target.closest<HTMLElement>('[data-comment-id]')
          const link = mark ? null : target.closest<HTMLAnchorElement>('.tiptap a[href]')
          if (mark) {
            e.stopPropagation()
            this._handleMarkClick(mark)
          } else if (link) {
            // Link-Popover (öffnen/bearbeiten/entfernen)
            e.preventDefault()
            e.stopPropagation()
            this._handleLinkClick(link)
            return
          } else if (!target.closest('.comment-popup')) {
            this.hideMarkPopup()
          }
          this.linkPopupVisible = false
        })
      }

      // Inline-Kommentar-Popup beim Scrollen des Editors ausblenden
      const scrollContainer = document.querySelector('.editor-container')
      if (scrollContainer) {
        scrollContainer.addEventListener(
          'scroll',
          () => {
            if (!this.commentPopupExpanded) {
              this.commentPopupVisible = false
            }
          },
          { passive: true },
        )
      }

      // Versionen laden, sobald der Verlauf-Tab geöffnet wird
      this.$watch('sidebarTab', (val) => {
        if (val === 'history' && this.revisions.length === 0) {
          void this.loadRevisions()
        }
      })

      // Bidirektionale Kommentar-Hervorhebung: CSS-Klasse auf der Editor-Markierung
      this.$watch('activeCommentId', (newId, oldId) => {
        const editorEl = document.getElementById('editor-container')
        if (!editorEl) return
        if (oldId) {
          const oldMark = editorEl.querySelector(`[data-comment-id="${oldId}"]`)
          if (oldMark) oldMark.classList.remove('comment-mark--active')
        }
        if (newId) {
          const newMark = editorEl.querySelector(`[data-comment-id="${newId}"]`)
          if (newMark) newMark.classList.add('comment-mark--active')
        }
      })
    },

    _initSoloEditor(editorEl: HTMLElement, initialContent: string): void {
      editor = window.MandariEditor.createEditor({
        element: editorEl,
        content: initialContent,
        editable: true,
        placeholder: PLACEHOLDER,
        onUpdate: () => {
          this.updatedAt = Date.now()
          this.updateWordCount()
        },
        onSelectionUpdate: (state) => {
          this.updatedAt = Date.now()
          this.formats = state
          this._checkTextSelection()
        },
        onPageCount: (count, currentPage) => {
          this.pageCount = count
          this.currentPage = currentPage
        },
        onSearchUpdate: (current, total) => {
          this.searchCurrent = current
          this.searchTotal = total
        },
      })
    },

    _initCollabEditor(editorEl: HTMLElement, initialContent: string, wsUrl: string): void {
      this._everConnected = false
      try {
        const result = window.MandariEditor.createCollaborativeEditor({
          element: editorEl,
          content: initialContent,
          editable: true,
          placeholder: PLACEHOLDER,
          wsUrl: wsUrl,
          user: {
            name: this.collabUserName,
            color: this.collabUserColor,
          },
          onUpdate: () => {
            this.updatedAt = Date.now()
            this.updateWordCount()
          },
          onSelectionUpdate: (state) => {
            this.updatedAt = Date.now()
            this.formats = state
            this._checkTextSelection()
          },
          onPageCount: (count, currentPage) => {
            this.pageCount = count
            this.currentPage = currentPage
          },
          onSearchUpdate: (current, total) => {
            this.searchCurrent = current
            this.searchTotal = total
          },
          onPresenceChange: (users) => {
            this.collabUsers = users
          },
          onStatusChange: (status) => {
            this.collabStatus = status
            if (status === 'connected') {
              this._everConnected = true
            } else if (status === 'disconnected' && !this._everConnected) {
              // Erste Verbindung fehlgeschlagen → stabiler Solo-Fallback
              this._switchToSoloMode('Echtzeit-Kollaboration nicht verfügbar — Solo-Modus aktiv.')
            }
          },
          onReloadRequired: () => {
            // Server hat eine Version wiederhergestellt → frisch laden
            showToast('Eine Version wurde wiederhergestellt — das Dokument wird neu geladen.', 'info')
            window.setTimeout(() => window.location.reload(), 800)
          },
        })
        editor = result.editor
        this.collabDestroy = result.collab.destroy
      } catch (e) {
        console.error('Kollaborations-Editor konnte nicht gestartet werden, Solo-Fallback:', e)
        this.collabEnabled = false
        this.collabStatus = 'disconnected'
        this._initSoloEditor(editorEl, initialContent)
      }
    },

    destroy(): void {
      // Hinweis: der globale Keydown-Handler bleibt bewusst registriert —
      // destroy() wird auch von den Editor-Recovery-Pfaden aufgerufen und
      // die Seite wird bei Navigation ohnehin komplett neu geladen.
      if (autoSaveInterval) {
        window.clearInterval(autoSaveInterval)
        autoSaveInterval = null
      }
      if (this.collabDestroy) {
        this.collabDestroy()
        this.collabDestroy = null
      }
      if (editor) {
        editor.destroy()
        editor = null
      }
    },

    _switchToSoloMode(reason = 'Kollaboration vorübergehend deaktiviert'): void {
      if (this._collabFailed) return
      this._collabFailed = true

      let currentText = ''
      if (editor && !editor.isDestroyed) {
        try {
          currentText = editor.getText() || ''
        } catch {
          currentText = ''
        }
      }

      this.destroy()
      this.collabEnabled = false
      this.collabStatus = 'disconnected'
      this.collabUsers = []

      const editorEl = document.getElementById('editor-container')
      if (!editorEl) return
      editorEl.innerHTML = ''

      const escaped = escapeHtml(currentText || '')
      const fallbackContent = escaped
        ? `<p>${escaped.replace(/\n/g, '<br>')}</p>`
        : document.getElementById('editor-initial-content')?.innerHTML || '<p></p>'
      this._initSoloEditor(editorEl, fallbackContent)
      showToast(reason, 'warning')
    },

    _rebuildSoloEditorFromText(): void {
      if (this._recoveringEditor) return
      this._recoveringEditor = true
      try {
        let text = ''
        if (editor && !editor.isDestroyed) {
          try {
            text = editor.getText() || ''
          } catch {
            text = ''
          }
        }
        this.destroy()
        this.collabEnabled = false
        this.collabStatus = 'disconnected'
        this.collabUsers = []

        const editorEl = document.getElementById('editor-container')
        if (!editorEl) return
        editorEl.innerHTML = ''

        const escaped = escapeHtml(text || '')
        const safeHtml = escaped ? `<p>${escaped.replace(/\n/g, '<br>')}</p>` : '<p></p>'
        this._initSoloEditor(editorEl, safeHtml)
      } finally {
        this._recoveringEditor = false
      }
    },

    closeShareModal(): void {
      this.showShareModal = false
      this.addUserEmail = ''
    },

    scrollToCommentInContent(commentId: string): void {
      if (!editor) return
      // Aktiven Kommentar setzen (bidirektionale Hervorhebung)
      this.activeCommentId = commentId
      this.sidebarTab = 'comments'

      const editorEl = document.getElementById('editor-container')
      if (!editorEl) return
      const mark = editorEl.querySelector(`[data-comment-id="${commentId}"]`)
      if (mark) {
        mark.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
    },

    replyToComment(commentId: string): void {
      this.replyingToComment = this.replyingToComment === commentId ? null : commentId
    },

    clearSelection(): void {
      this.selectedText = ''
      this.selectionStart = 0
      this.selectionEnd = 0
    },

    // === Inline-Kommentar-Popup ===

    _checkTextSelection(): void {
      if (!editor) return
      // Nicht aktualisieren, solange ein Kommentar getippt oder ein Mark-Kommentar angezeigt wird
      if (this.commentPopupExpanded) return
      if (this.markCommentPopup) return

      const { from, to, empty } = editor.state.selection

      if (empty || from === to) {
        this.commentPopupVisible = false
        return
      }

      const selectedText = editor.state.doc.textBetween(from, to, ' ')
      if (!selectedText.trim()) {
        this.commentPopupVisible = false
        return
      }

      // Popup-Position berechnen (viewport-relativ für position: fixed)
      try {
        const endCoords = editor.view.coordsAtPos(to)
        this.commentPopupTop = endCoords.bottom + 8
        // Überlauf rechts vermeiden
        this.commentPopupLeft = Math.min(endCoords.left, window.innerWidth - 350)
        // Überlauf unten vermeiden — über die Auswahl klappen
        if (this.commentPopupTop + 200 > window.innerHeight) {
          const startCoords = editor.view.coordsAtPos(from)
          this.commentPopupTop = startCoords.top - 48
        }
      } catch {
        return
      }

      this.inlineSelectedText = selectedText
      this._selectionFrom = from
      this._selectionTo = to
      this.commentPopupVisible = true
    },

    expandCommentPopup(): void {
      this.commentPopupExpanded = true
      void this.$nextTick(() => {
        const input = this.$refs.inlineCommentInput
        if (input) input.focus()
      })
    },

    hideCommentPopup(): void {
      this.commentPopupVisible = false
      this.commentPopupExpanded = false
      this.inlineCommentText = ''
      this.inlineSelectedText = ''
    },

    // === Kommentardetails (Klick auf bestehende Markierung) ===

    _handleMarkClick(markEl: HTMLElement): void {
      const markId = markEl.getAttribute('data-comment-id')
      if (!markId) return

      const comment = this.inlineCommentsData.find((c) => c.mark_id === markId)
      if (!comment) return

      // Erneuter Klick auf dieselbe Markierung schaltet aus
      if (this.activeCommentId === markId) {
        this.activeCommentId = null
        this.hideMarkPopup()
        return
      }

      this.activeCommentId = markId

      // Erstellungs-Popup ausblenden, falls sichtbar
      this.commentPopupVisible = false

      // Popup nahe der Markierung positionieren
      const rect = markEl.getBoundingClientRect()
      this.markCommentTop = rect.bottom + 8
      this.markCommentLeft = Math.min(rect.left, window.innerWidth - 340)
      if (this.markCommentTop + 300 > window.innerHeight) {
        this.markCommentTop = rect.top - 280
      }

      this.markCommentData = comment
      this.markCommentPopup = true
      this.markReplyText = ''

      // Zum Kommentar-Tab wechseln und Sidebar zu diesem Kommentar scrollen
      this.sidebarTab = 'comments'
      void this.$nextTick(() => {
        const sidebarComment = document.getElementById(`comment-sidebar-${comment.id}`)
        if (sidebarComment) {
          sidebarComment.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }
      })
    },

    hideMarkPopup(): void {
      this.markCommentPopup = false
      this.markCommentData = null
      this.markReplyText = ''
      this.activeCommentId = null
    },

    async submitMarkReply(): Promise<void> {
      if (!this.markReplyText.trim() || this.markReplySubmitting || !this.markCommentData) return
      this.markReplySubmitting = true

      const formData = new URLSearchParams()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('content', this.markReplyText.trim())
      formData.append('parent', this.markCommentData.id)

      try {
        const response = await fetch(config.urls.comment, {
          method: 'POST',
          headers: formHeaders(),
          body: formData,
        })
        const data: JsonResponse = await response.json()
        if (data.success && data.comment) {
          // Antwort lokal ergänzen, damit das Popup sofort aktuell ist
          this.markCommentData.replies.push({
            id: data.comment.id,
            content: data.comment.content,
            author_name: data.comment.author,
            author_initials: String(data.comment.author).substring(0, 2).toUpperCase(),
            created_at: data.comment.created_at,
          })
          this.markReplyText = ''
        } else {
          showToast('Fehler beim Speichern der Antwort.', 'error')
        }
      } catch (error) {
        console.error('Reply error:', error)
        showToast('Fehler beim Speichern der Antwort.', 'error')
      }

      this.markReplySubmitting = false
    },

    async resolveMarkComment(): Promise<void> {
      if (!this.markCommentData) return
      const commentId = this.markCommentData.id

      try {
        const response = await fetch(`${config.urls.comment}${commentId}/resolve/`, {
          method: 'POST',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': csrfToken(),
          },
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          this.markCommentData.is_resolved = true
          // Markierung im Editor entfernen (erledigt)
          if (editor) {
            editor.commands.unsetCommentMark(this.markCommentData.mark_id)
          }
          this.hideMarkPopup()
          await this.reloadCommentsSidebar()
        } else {
          showToast('Fehler beim Erledigen.', 'error')
        }
      } catch {
        showToast('Fehler beim Erledigen.', 'error')
      }
    },

    async submitInlineComment(): Promise<void> {
      if (!this.inlineCommentText.trim() || this.commentSubmitting) return
      this.commentSubmitting = true

      // mark_id clientseitig erzeugen und Markierung SYNCHRON setzen — nach einem
      // await wären die Positionen veraltet ("mismatched transaction")
      const markId = crypto.randomUUID()
      let markApplied = false

      if (editor) {
        try {
          editor
            .chain()
            .focus()
            .setTextSelection({ from: this._selectionFrom, to: this._selectionTo })
            .setCommentMark({ commentId: markId })
            .run()
          markApplied = true
        } catch (e) {
          console.warn('Failed to apply comment mark:', e)
        }
      }

      const formData = new URLSearchParams()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('content', this.inlineCommentText.trim())
      formData.append('selected_text', this.inlineSelectedText)
      formData.append('selection_start', String(this._selectionFrom))
      formData.append('selection_end', String(this._selectionTo))
      formData.append('mark_id', markId)

      const revertMark = () => {
        if (markApplied && editor) {
          try {
            editor.commands.unsetCommentMark(markId)
          } catch {
            /* ignorieren */
          }
        }
      }

      try {
        const response = await fetch(config.urls.comment, {
          method: 'POST',
          headers: formHeaders(),
          body: formData,
        })
        const data: JsonResponse = await response.json()
        if (data.success && data.comment) {
          this.hideCommentPopup()
          this.sidebarTab = 'comments'
          await this.reloadCommentsSidebar()
        } else {
          revertMark()
          showToast('Fehler beim Speichern des Kommentars.', 'error')
        }
      } catch (error) {
        console.error('Inline comment error:', error)
        revertMark()
        showToast('Fehler beim Speichern des Kommentars.', 'error')
      }

      this.commentSubmitting = false
    },

    async submitComment(): Promise<void> {
      if (!this.newCommentContent.trim() || this.commentSubmitting) return
      this.commentSubmitting = true

      const formData = new URLSearchParams()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('content', this.newCommentContent.trim())
      if (this.selectedText) {
        formData.append('selected_text', this.selectedText)
        formData.append('selection_start', String(this.selectionStart))
        formData.append('selection_end', String(this.selectionEnd))
      }

      try {
        const response = await fetch(config.urls.comment, {
          method: 'POST',
          headers: formHeaders(),
          body: formData,
        })
        const data: JsonResponse = await response.json()
        if (data.success && data.comment) {
          // Inline-Kommentar mit mark_id: Markierung im Editor setzen
          if (data.comment.mark_id && editor && this.selectedText) {
            editor.chain().focus().setCommentMark({ commentId: data.comment.mark_id }).run()
          }
          this.newCommentContent = ''
          this.clearSelection()
          await this.reloadCommentsSidebar()
        } else {
          showToast('Fehler beim Speichern des Kommentars.', 'error')
        }
      } catch (error) {
        console.error('Comment error:', error)
        showToast('Fehler beim Speichern des Kommentars.', 'error')
      }

      this.commentSubmitting = false
    },

    async reloadCommentsSidebar(): Promise<void> {
      try {
        const response = await fetch(window.location.href, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
        if (!response.ok) return
        const html = await response.text()
        const parser = new DOMParser()
        const doc = parser.parseFromString(html, 'text/html')
        const selector = `.editor-sidebar [x-show="sidebarTab === 'comments'"]`
        const nextSidebar = doc.querySelector(selector)
        const currentSidebar = document.querySelector(selector)
        if (nextSidebar && currentSidebar) {
          currentSidebar.innerHTML = nextSidebar.innerHTML
        }
      } catch (error) {
        console.warn('Could not refresh comments sidebar:', error)
      }
    },

    updateWordCount(): void {
      if (!editor) return
      const text = editor.getText().trim()
      this.wordCount = text ? text.split(/\s+/).length : 0
      try {
        this.charCount = editor.storage.characterCount ? editor.storage.characterCount.characters() : text.length
      } catch {
        this.charCount = text.length
      }
    },

    /* === Formatvorlagen-Label (folgt dem Cursor) === */
    get styleLabel(): string {
      if (this.formats.header) return 'Überschrift ' + this.formats.header
      if (this.formats.blockquote) return 'Zitat'
      return 'Standard'
    },

    /* === Suchen & Ersetzen === */
    toggleSearch(): void {
      if (this.searchOpen) {
        this.closeSearch()
      } else {
        this.openSearch()
      }
    },

    openSearch(): void {
      this.searchOpen = true
      void this.$nextTick(() => {
        const input = this.$refs.searchInput as HTMLInputElement | undefined
        if (input) {
          input.focus()
          input.select()
        }
      })
      if (this.searchTerm) this.runSearch()
    },

    closeSearch(): void {
      this.searchOpen = false
      this.searchCurrent = 0
      this.searchTotal = 0
      if (editor && !editor.isDestroyed) {
        editor.commands.clearSearch()
      }
    },

    runSearch(): void {
      if (!editor || editor.isDestroyed) return
      if (!this.searchTerm) {
        editor.commands.clearSearch()
        this.searchCurrent = 0
        this.searchTotal = 0
        return
      }
      editor.commands.setSearchTerm(this.searchTerm, this.searchMatchCase)
    },

    findNext(): void {
      if (!editor || editor.isDestroyed) return
      editor.commands.findNext()
    },

    findPrev(): void {
      if (!editor || editor.isDestroyed) return
      editor.commands.findPrevious()
    },

    replaceCurrent(): void {
      if (!editor || editor.isDestroyed || !this.searchTotal) return
      editor.commands.replaceCurrent(this.replaceTerm)
    },

    replaceAll(): void {
      if (!editor || editor.isDestroyed || !this.searchTotal) return
      editor.commands.replaceAll(this.replaceTerm)
    },

    /* === Links: Dialog + Popover === */
    openLinkDialog(): void {
      if (!editor || editor.isDestroyed) return
      this.linkPopupVisible = false
      this.linkUrl = editor.getAttributes('link').href || ''
      this.showLinkModal = true
      void this.$nextTick(() => {
        if (this.$refs.linkUrlInput) this.$refs.linkUrlInput.focus()
      })
    },

    insertLink(): void {
      if (!editor || editor.isDestroyed) return
      let url = this.linkUrl.trim()
      if (!url) return
      if (!/^(https?:|mailto:|tel:)/i.test(url)) {
        url = 'https://' + url
      }
      try {
        editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
      } catch (err) {
        console.error('Link insert failed:', err)
      }
      this.showLinkModal = false
      this.linkUrl = ''
    },

    removeLink(): void {
      if (!editor || editor.isDestroyed) return
      try {
        editor.chain().focus().extendMarkRange('link').unsetLink().run()
      } catch (err) {
        console.error('Link remove failed:', err)
      }
      this.showLinkModal = false
      this.linkUrl = ''
    },

    _handleLinkClick(linkEl: HTMLAnchorElement): void {
      this.linkPopupHref = linkEl.getAttribute('href') || ''
      const rect = linkEl.getBoundingClientRect()
      this.linkPopupTop = rect.bottom + 6
      this.linkPopupLeft = Math.max(8, rect.left)
      this.linkPopupVisible = true
    },

    editLinkFromPopup(): void {
      this.linkPopupVisible = false
      this.linkUrl = this.linkPopupHref
      this.showLinkModal = true
      void this.$nextTick(() => {
        if (this.$refs.linkUrlInput) this.$refs.linkUrlInput.focus()
      })
    },

    removeLinkFromPopup(): void {
      this.linkPopupVisible = false
      this.removeLink()
    },

    runEditorCommand(commandFn: (editor: Editor) => void): void {
      if (!editor || editor.isDestroyed) return
      try {
        commandFn(editor)
      } catch (error) {
        console.error('Editor command failed:', error)
      }
    },

    isEditorReady(): boolean {
      return !!editor && !editor.isDestroyed
    },

    _bindToolbarCommands(): void {
      const toolbar = document.getElementById('editor-toolbar')
      if (!toolbar) return

      toolbar.addEventListener('mousedown', (event) => {
        const button = (event.target as Element | null)?.closest<HTMLElement>('[data-editor-cmd]')
        if (!button) return
        event.preventDefault()
        if (!editor || editor.isDestroyed) return

        const cmd = button.getAttribute('data-editor-cmd') || ''
        const level = Number.parseInt(button.getAttribute('data-editor-level') || '0', 10)
        const value = button.getAttribute('data-editor-value') || ''
        this._executeToolbarCommand(cmd, level, value)
      })
    },

    _executeToolbarCommand(cmd: string, level = 0, value = ''): void {
      if (!editor || editor.isDestroyed) return
      try {
        switch (cmd) {
          case 'bold':
            editor.chain().focus().toggleBold().run()
            break
          case 'italic':
            editor.chain().focus().toggleItalic().run()
            break
          case 'underline':
            editor.chain().focus().toggleUnderline().run()
            break
          case 'strike':
            editor.chain().focus().toggleStrike().run()
            break
          case 'highlight':
            editor.chain().focus().toggleHighlight().run()
            break
          case 'paragraph':
            editor.chain().focus().setParagraph().run()
            break
          case 'align':
            if (!value) break
            editor.chain().focus().setTextAlign(value).run()
            break
          case 'pageBreak':
            editor.chain().focus().setPageBreak().run()
            break
          case 'heading':
            if (!level) break
            editor
              .chain()
              .focus()
              .toggleHeading({ level: level as 1 | 2 | 3 })
              .run()
            break
          case 'bulletList':
            editor.chain().focus().toggleBulletList().run()
            break
          case 'orderedList':
            editor.chain().focus().toggleOrderedList().run()
            break
          case 'taskList':
            editor.chain().focus().toggleTaskList().run()
            break
          case 'blockquote':
            editor.chain().focus().toggleBlockquote().run()
            break
          case 'horizontalRule':
            editor.chain().focus().setHorizontalRule().run()
            break
          case 'indent':
            // Listen: Listenelement einrücken; sonst Absatz-Einzug
            if (editor.isActive('listItem') && editor.can().sinkListItem('listItem')) {
              editor.chain().focus().sinkListItem('listItem').run()
            } else {
              editor.chain().focus().indent().run()
            }
            break
          case 'outdent':
            if (editor.isActive('listItem') && editor.can().liftListItem('listItem')) {
              editor.chain().focus().liftListItem('listItem').run()
            } else {
              editor.chain().focus().outdent().run()
            }
            break
          case 'undo':
            editor.commands.undo()
            break
          case 'redo':
            editor.commands.redo()
            break
        }
      } catch (error) {
        console.error('Editor command failed:', error)
      }
    },

    _bindExtendedToolbar(): void {
      const wrapper = this.$el

      // Tabellenbefehle per Custom-Event ($dispatch steigt bis zum Wrapper auf)
      wrapper.addEventListener('editor-table-cmd', (e) => {
        if (!editor || editor.isDestroyed) return
        const cmd = (e as CustomEvent<string>).detail
        try {
          switch (cmd) {
            case 'insertTable':
              editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()
              break
            case 'addColumnBefore':
              editor.chain().focus().addColumnBefore().run()
              break
            case 'addColumnAfter':
              editor.chain().focus().addColumnAfter().run()
              break
            case 'addRowBefore':
              editor.chain().focus().addRowBefore().run()
              break
            case 'addRowAfter':
              editor.chain().focus().addRowAfter().run()
              break
            case 'deleteColumn':
              editor.chain().focus().deleteColumn().run()
              break
            case 'deleteRow':
              editor.chain().focus().deleteRow().run()
              break
            case 'deleteTable':
              editor.chain().focus().deleteTable().run()
              break
          }
        } catch (err) {
          console.error('Table command failed:', err)
        }
      })

      // Textfarbe per Custom-Event
      wrapper.addEventListener('editor-color-cmd', (e) => {
        if (!editor || editor.isDestroyed) return
        const color = (e as CustomEvent<string>).detail
        try {
          if (color) {
            editor.chain().focus().setColor(color).run()
          } else {
            editor.chain().focus().unsetColor().run()
          }
        } catch (err) {
          console.error('Color command failed:', err)
        }
      })

      // Bild einfügen aus dem Slash-Menü (Event vom Editor-Bundle auf window)
      window.addEventListener('slash-insert-image', () => {
        this.showImageModal = true
      })
    },

    insertImage(): void {
      if (!editor || !this.imageUrl.trim()) return
      try {
        editor
          .chain()
          .focus()
          .setImage({
            src: this.imageUrl.trim(),
            alt: this.imageAlt.trim() || undefined,
          })
          .run()
      } catch (err) {
        console.error('Image insert failed:', err)
      }
      this.showImageModal = false
      this.imageUrl = ''
      this.imageAlt = ''
    },

    getContent(): string {
      if (!editor) return ''
      return editor.getHTML()
    },

    setContent(html: string): void {
      if (!editor) return
      editor.commands.setContent(html)
    },

    async save(): Promise<void> {
      if (this.saving || !editor) return
      this.saving = true
      this.saveError = false

      const formData = new FormData()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('action', 'save')
      formData.append('title', this.title)
      formData.append('content', this.getContent())
      formData.append('motion_type', this.motionType)
      formData.append('document_type_id', this.documentTypeId)
      formData.append('letterhead_id', this.letterheadId)

      try {
        const response = await fetch(window.location.href, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body: formData,
        })

        if (response.ok) {
          const data: JsonResponse = await response.json()
          if (data.success) {
            this.lastSaved = new Date().toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
            this.saveError = false
          } else {
            this.saveError = true
          }
        } else {
          this.saveError = true
        }
      } catch (error) {
        this.saveError = true
        console.error('Save error:', error)
      }

      this.saving = false
    },

    autoSave(): void {
      // Im Kollab-Modus persistiert der Server (yjs_save inkl. HTML +
      // gedrosselte Revisionen) — kein POST-Autosave nötig, solange
      // die Verbindung steht.
      if (this.collabEnabled && this.collabStatus === 'connected') return
      if (!this.saving && editor) void this.save()
    },

    setDocumentType(id: string, name: string): void {
      this.documentTypeId = id
      this.documentTypeName = name
    },

    setLetterhead(id: string, name: string): void {
      this.letterheadId = id
      this.letterheadName = name

      // Briefkopf-Hintergrund neu rendern
      const container = document.getElementById('editor-container')
      if (!container) return

      // Vorhandenes Canvas und HTML-Vorschau entfernen
      const existingCanvas = container.querySelector('.letterhead-canvas')
      if (existingCanvas) existingCanvas.remove()
      this._removeGeneratedLetterhead(container)

      // Editor-Padding + Schrift auf Standard zurücksetzen (WYSIWYG-Fallback)
      const tiptapEl = container.querySelector<HTMLElement>('.tiptap')
      if (tiptapEl) {
        tiptapEl.style.padding = '30mm 25mm 25mm 25mm'
        tiptapEl.style.fontFamily = 'Arial, Helvetica, sans-serif'
        tiptapEl.style.fontSize = '11pt'
      }

      if (!id) return // "Kein Briefpapier" gewählt

      const lhData = this.letterheadsData.find((lh) => lh.id === id)
      if (!lhData) return

      // Schrift aus dem Briefkopf anwenden (WYSIWYG = Export)
      if (tiptapEl) {
        if (lhData.font_family) {
          tiptapEl.style.fontFamily = `"${lhData.font_family}", Arial, Helvetica, sans-serif`
        }
        if (lhData.font_size) {
          tiptapEl.style.fontSize = `${lhData.font_size}pt`
        }
        // Ränder direkt setzen — auch wenn keine Vorschau geladen werden kann
        tiptapEl.style.padding = `${lhData.margin_top}mm ${lhData.margin_right}mm ${lhData.margin_bottom}mm ${lhData.margin_left}mm`
      }

      const margins: LetterheadMargins = {
        top: lhData.margin_top,
        right: lhData.margin_right,
        bottom: lhData.margin_bottom,
        left: lhData.margin_left,
      }

      if (lhData.kind === 'generated') {
        // Generierter Briefkopf: HTML-Vorschau statt pdfjs-Overlay
        void this._renderGeneratedLetterhead(lhData.preview_url, margins)
        return
      }

      const MandariEditor = window.MandariEditor
      if (!MandariEditor?.renderLetterhead) return

      void MandariEditor.renderLetterhead({
        pdfUrl: lhData.pdf_url,
        container: container,
        opacity: 0.25,
        margins,
      })
    },

    async _renderGeneratedLetterhead(previewUrl: string, margins: LetterheadMargins): Promise<void> {
      const container = document.getElementById('editor-container')
      if (!container || !previewUrl) return

      this._removeGeneratedLetterhead(container)

      try {
        const response = await fetch(previewUrl)
        if (!response.ok) return
        const html = await response.text()

        const wrapper = document.createElement('div')
        wrapper.className = 'letterhead-html-preview'
        wrapper.style.padding = `12mm ${margins.right}mm 0 ${margins.left}mm`
        wrapper.innerHTML = html
        container.insertBefore(wrapper, container.firstChild)

        // Inhalt rückt unter den Briefkopf: oberes Padding reduzieren
        const tiptapEl = container.querySelector<HTMLElement>('.tiptap')
        if (tiptapEl) {
          tiptapEl.style.padding = `6mm ${margins.right}mm ${margins.bottom}mm ${margins.left}mm`
        }
      } catch {
        // Vorschau ist optional — Fehler still ignorieren
      }
    },

    _removeGeneratedLetterhead(container: Element): void {
      const existing = container.querySelector('.letterhead-html-preview')
      if (existing) existing.remove()
    },

    async changeStatus(newStatus: string): Promise<void> {
      try {
        const response = await fetch(config.urls.status, {
          method: 'POST',
          headers: formHeaders(),
          body: new URLSearchParams({ status: newStatus }),
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          location.reload()
        } else {
          showToast('Fehler: ' + (data.error || 'Unbekannter Fehler'), 'error')
        }
      } catch {
        showToast('Fehler beim Statuswechsel', 'error')
      }
    },

    async confirmDelete(): Promise<void> {
      const ok = await confirmAction({
        title: 'Dokument löschen',
        message: 'Möchten Sie dieses Dokument wirklich löschen? Es wird für 30 Tage im Papierkorb aufbewahrt.',
        confirmText: 'Löschen',
        variant: 'danger',
      })
      if (!ok) return

      const formData = new FormData()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('action', 'delete')

      try {
        const response = await fetch(window.location.href, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body: formData,
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          window.location.href = config.urls.documents
        } else {
          showToast('Fehler beim Loeschen: ' + (data.error || 'Unbekannter Fehler'), 'error')
        }
      } catch {
        showToast('Fehler beim Loeschen des Dokuments.', 'error')
      }
    },

    sendMessage(): void {
      if (!this.userMessage.trim() || this.aiLoading) return
      const msg = this.userMessage.trim()
      this.chatMessages.push({ role: 'user', content: msg })
      this.userMessage = ''
      void this.aiAction('chat', msg)
    },

    async aiAction(action: string, instruction = ''): Promise<void> {
      this.aiLoading = true
      void this.$nextTick(() => {
        const container = this.$refs.chatMessages
        if (container) container.scrollTop = container.scrollHeight
      })

      const text = editor ? this.getContent() : ''
      const selectedText = this.selectedText || ''
      const historyPayload = this.chatMessages
        .filter((m) => m.role === 'user' || m.role === 'ai')
        .slice(-8)
        .map((m) => ({
          role: m.role === 'ai' ? 'assistant' : 'user',
          content: String(m.content || '')
            .replace(/<[^>]+>/g, ' ')
            .replace(/\s+/g, ' ')
            .trim(),
        }))

      try {
        const response = await fetch(config.urls.ai, {
          method: 'POST',
          headers: formHeaders(),
          body: new URLSearchParams({
            action: action,
            text: text,
            instruction: instruction,
            motion_type: this.motionType,
            selected_text: selectedText,
            history: JSON.stringify(historyPayload),
          }),
        })

        const data: JsonResponse = await response.json()
        if (data.success) {
          // Kontingent-Anzeige aktualisieren
          if (data.quota) {
            this.aiQuotaLimit = data.quota.limit === undefined ? this.aiQuotaLimit : data.quota.limit
            this.aiQuotaUsed = data.quota.used === undefined ? this.aiQuotaUsed : data.quota.used
          }
          let content = ''
          let hasAction = false
          let actionContent: string | null = null

          if (data.content) {
            content = String(data.content).replace(/\n/g, '<br>')
            hasAction = action !== 'chat'
            actionContent = data.content
          }
          if (Array.isArray(data.suggestions) && data.suggestions.length > 0) {
            content += '<ul class="mt-2 space-y-1">'
            for (const s of data.suggestions) {
              content += `<li class="text-sm">${s}</li>`
            }
            content += '</ul>'
          }
          if (!content) content = 'Keine Vorschlaege verfuegbar.'

          this.chatMessages.push({ role: 'ai', content, hasAction, actionContent })
        } else {
          this.chatMessages.push({
            role: 'ai',
            content: 'Entschuldigung, es ist ein Fehler aufgetreten: ' + (data.error || 'Unbekannter Fehler'),
          })
        }
      } catch {
        this.chatMessages.push({
          role: 'ai',
          content: 'Entschuldigung, die Verbindung zum KI-Service ist fehlgeschlagen.',
        })
      }

      this.aiLoading = false
      void this.$nextTick(() => {
        const container = this.$refs.chatMessages
        if (container) container.scrollTop = container.scrollHeight
      })
    },

    applyAiContent(content: string | null | undefined): void {
      if (content && editor) {
        this.aiOriginalContent = this.getContent()
        this.aiPreviewContent = content
        this.previewMode = 'suggested'
        this.aiPreviewActive = true
      }
    },

    closeAiPreview(): void {
      this.aiPreviewActive = false
      this.aiPreviewContent = ''
      this.aiOriginalContent = ''
      this.previewMode = 'suggested'
    },

    applyAiPreview(): void {
      if (this.aiPreviewContent && editor) {
        this.setContent(this.aiPreviewContent)
        this.chatMessages.push({ role: 'ai', content: 'Änderungen wurden in den Editor übernommen.' })
        this.closeAiPreview()
      }
    },

    // === Versionsverlauf ===

    async loadRevisions(): Promise<void> {
      if (this.revisionsLoading) return
      this.revisionsLoading = true

      try {
        const response = await fetch(config.urls.revisions, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          this.revisions = data.revisions
        }
      } catch (error) {
        console.error('Error loading revisions:', error)
      }

      this.revisionsLoading = false
    },

    async viewRevision(rev: Revision): Promise<void> {
      this.selectedRevision = rev
      this.historyMode = true
      this.revisionLoading = true
      this.revisionViewMode = 'diff'

      try {
        const response = await fetch(`${config.urls.revisions}${rev.id}/`, {
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          this.revisionContent = data.revision.content
          // Diff zwischen Version und aktuellem Inhalt erzeugen
          const currentContent = editor ? this.getContent() : ''
          const MandariEditor = window.MandariEditor
          if (MandariEditor?.renderDiff) {
            this.revisionDiff = MandariEditor.renderDiff(data.revision.content, currentContent)
          } else {
            this.revisionDiff = data.revision.content
          }
        }
      } catch (error) {
        console.error('Error loading revision:', error)
      }

      this.revisionLoading = false
    },

    exitHistoryMode(): void {
      this.historyMode = false
      this.selectedRevision = null
      this.revisionDiff = ''
      this.revisionContent = ''
    },

    async restoreRevision(rev: Revision): Promise<void> {
      const ok = await confirmAction({
        title: 'Version wiederherstellen',
        message: `Moechten Sie Version ${rev.version} wiederherstellen? Der aktuelle Inhalt wird als neue Version gesichert.`,
        confirmText: 'Wiederherstellen',
        variant: 'info',
      })
      if (!ok) return

      try {
        const response = await fetch(`${config.urls.revisions}${rev.id}/restore/`, {
          method: 'POST',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': csrfToken(),
          },
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          // Neu laden, um den wiederhergestellten Inhalt zu bekommen
          location.reload()
        } else {
          showToast('Fehler: ' + (data.error || 'Unbekannter Fehler'), 'error')
        }
      } catch {
        showToast('Fehler beim Wiederherstellen', 'error')
      }
    },

    formatTimeAgo(isoDate: string): string {
      const date = new Date(isoDate)
      const now = new Date()
      const diffMs = now.getTime() - date.getTime()
      const diffMin = Math.floor(diffMs / 60000)
      const diffHours = Math.floor(diffMs / 3600000)
      const diffDays = Math.floor(diffMs / 86400000)

      if (diffMin < 1) return 'gerade eben'
      if (diffMin < 60) return `vor ${diffMin} Min.`
      if (diffHours < 24) return `vor ${diffHours} Std.`
      if (diffDays < 7) return `vor ${diffDays} Tagen`
      return date.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
    },
  }
})
