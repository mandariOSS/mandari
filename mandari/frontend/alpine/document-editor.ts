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
import { type AiTarget, applyAiSuggestion, captureAiTarget, textOfRange } from '../editor/ai-range'
import type { CollabUser } from '../editor/collaboration'
import type { FormatState } from '../editor/index'
import { type GliederungsEintrag, gliederungAus, randAnordnen, zuUeberschriftSpringen } from '../editor/rand'
import { inEinemAbsatz, vorschlagPasst, vorschlagUebernehmen } from '../editor/vorschlag'
import { defineComponent } from '../js/alpine/component'
import { confirmAction } from '../js/alpine/confirm-dialog'
import { showToast } from '../js/alpine/toast'
import { csrfToken, csrfTokenAktuell } from '../js/csrf'
import { readJsonScript } from '../js/json-script'
import { bereinigeKiHtml } from '../js/ki-ausgabe'
import { installMotionTracking } from '../js/motion-tracking'
import { navigateTo } from '../js/navigation'

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
  /** Mitgliedschaft der Verfasserin bzw. des Verfassers (erledigen darf sie bzw. er) */
  author_id?: string
  mark_id: string
  content: string
  selected_text: string
  author_name: string
  author_initials: string
  created_at: string
  is_resolved: boolean
  replies: InlineCommentReply[]
  /** Änderungsvorschlag (#856): Ersatz für `selected_text` (leer = streichen); fehlt bzw. null = Kommentar */
  vorschlag?: string | null
  vorschlag_angenommen?: boolean | null
  /** Eigene Notiz zum Vorschlag (leer, wenn der Kommentartext nur den Vorschlag beschreibt) */
  notiz?: string
}

export interface DocumentEditorConfig {
  motionId: string
  title: string
  visibility: string
  /** Fingerabdruck des gespeicherten Inhalts für die Konflikterkennung (#184) */
  contentHash: string
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
  /** „neu“: Antragseditor mit Kommentaren am Rand und Ablauf in der Kopfzeile (Teil von #856); sonst „bisher“ */
  ansicht?: 'neu' | 'bisher'
  /** Status des Dokuments beim Laden */
  status?: string
  /** Eigene Mitgliedschaft und ob sie alle Kommentare erledigen darf (motions.edit_all) */
  membershipId?: string
  erledigenAlle?: boolean
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
    /** POST: Zustimmung (Freigabe) bei einem Mitglied anfragen */
    approvalRequest?: string
  }
}

/** Bereiche der rechten Spalte im neuen Editor: leer = Kommentare am Rand */
export type EditorPanel = '' | 'kommentare' | 'details' | 'aufgaben' | 'ki' | 'verlauf'
/** Schritte des Ablaufs, die einen Dialog öffnen */
export type AblaufDialog = '' | 'abstimmung' | 'freigeben' | 'einreichen'
/** Modus des neuen Editors: Text ändern, Änderungen vorschlagen (am Rand annehmen/ablehnen) oder nur lesen */
export type EditorModus = 'bearbeiten' | 'vorschlagen' | 'ansehen'
/** Speichern gestört und wird wiederholt (#927-Muster): Verbindung, Server oder abgelaufene Anmeldung */
export type SpeicherStoerung = '' | 'verbindung' | 'server' | 'anmeldung'

// ---- Zustandstypen ----------------------------------------------------------------

export interface ChatMessage {
  role: 'user' | 'ai'
  content: string
  hasAction?: boolean
  actionContent?: string | null
  /** Markierte Stelle, für die der Vorschlag gilt (Schlüssel in `aiTargets`) */
  targetId?: number | null
  /** Hinweis unter dem Vorschlag, z. B. warum er sich nicht übernehmen lässt */
  hint?: string
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
/** Vorauswahl „Wer stimmt ab“ im Dialog „Zur Abstimmung geben“ (json_script im neuen Editor) */
const ABSTIMMUNG_ID = 'editor-abstimmung-vorauswahl'
/** Einstellungen des neuen Editors je Browser */
const SPEICHER_GLIEDERUNG = 'mandari.editor.gliederung'
const SPEICHER_SEITENANSICHT = 'mandari.editor.seitenansicht'
/** Ab dieser Breite stehen Kommentare am Rand neben dem Text (Tailwind lg) */
const RAND_AB = '(min-width: 1024px)'
/** Status, in denen der neue Editor im Modus „Vorschlagen“ öffnet (Abstimmung in der Fraktion) */
const ABSTIMMUNG_STATUS = ['internal_review', 'external_review']

// Speichern mit Wiederholung wie in der Sitzungsvorbereitung (frontend/js/speichern.ts, #927): Störungen werden
// mit steigender Wartezeit wiederholt, solange die Seite offen ist, 500 bis zu dreimal, bei abgelaufener Anmeldung
// alle 30 s und sobald die Seite wieder Fokus hat. Jeder Versuch schickt den aktuellen Stand.
const WARTEZEITEN_MS = [1000, 2000, 4000, 8000, 15000, 30000]
const ANMELDUNG_WARTEZEIT_MS = 30000
const ZEITLIMIT_MS = 30000
const VORUEBERGEHEND = new Set([408, 425, 429, 502, 503, 504])
const VERSUCHE_BEI_500 = 3

type SpeicherAntwort =
  | { art: 'ok'; daten: JsonResponse }
  | { art: 'konflikt'; daten: JsonResponse }
  | { art: 'stoerung' | 'server' | 'anmeldung' | 'fehler' }

async function speicherAntwort(response: Response): Promise<SpeicherAntwort> {
  const json = (response.headers.get('Content-Type') || '').includes('application/json')
  const daten: JsonResponse | null = json ? await response.json().catch(() => null) : null
  // Abgelaufene Anmeldung: Django leitet auf die Anmeldeseite um, fetch folgt und liefert HTML
  if ((response.redirected && !json) || response.status === 401) return { art: 'anmeldung' }
  if (response.status === 403 && daten?.error === 'two_factor_setup_required') return { art: 'anmeldung' }
  if (response.status === 409) return { art: 'konflikt', daten: daten ?? {} }
  if (VORUEBERGEHEND.has(response.status)) return { art: 'stoerung' }
  if (response.status === 500) return { art: 'server' }
  if (response.ok && daten?.success) return { art: 'ok', daten }
  return { art: 'fehler' }
}

function gespeicherteEinstellung(key: string, standard: boolean): boolean {
  try {
    const wert = window.localStorage.getItem(key)
    return wert === null ? standard : wert === 'an'
  } catch {
    return standard
  }
}

function einstellungMerken(key: string, wert: boolean): void {
  try {
    window.localStorage.setItem(key, wert ? 'an' : 'aus')
  } catch {
    // Privater Modus o. Ä.: Einstellung gilt nur für diese Seite
  }
}

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
  // Markierte Stellen der KI-Vorschläge (Teil von #856): außerhalb des reaktiven Zustands, weil sie
  // Editor-Positionen und Ereignis-Abos halten
  const aiTargets = new Map<number, AiTarget>()
  let aiTargetSeq = 0
  // Neuer Editor: Gliederung und Rand nach Änderungen gebündelt neu berechnen
  let nachAenderungTimer: number | null = null
  let randFrame: number | null = null
  // Speichern mit Wiederholung: geplanter Versuch, Zahl der Versuche (500 zählt getrennt)
  let wiederholTimer: number | null = null
  let speicherVersuche = 0
  let serverfehler = 0
  // Stellen offener Änderungsvorschläge im Text anders hervorheben als Kommentare (die Marke selbst kennt den
  // Unterschied nicht): eigenes Stylesheet mit den Marken-IDs, ohne Inline-Stil im Template
  let vorschlagStil: CSSStyleSheet | null = null

  function releaseAiTargets(): void {
    for (const target of aiTargets.values()) target.release()
    aiTargets.clear()
  }

  return {
    updatedAt: Date.now(),
    title: config.title,
    wordCount: 0,
    charCount: 0,
    pageCount: 1,
    currentPage: 1,
    lastSaved: null as string | null,
    saveError: false,
    // Speichern ohne Verbindung traf auf einen neueren Stand (#184)
    saveConflict: false,
    contentHash: config.contentHash,
    saving: false,
    // Ungespeicherte Änderungen (#185): Zähler statt Boolean, damit Eingaben während eines
    // laufenden Speicherns nicht als gesichert gelten – gesichert ist erst der Stand, der beim
    // Absenden vorlag und den der Server bestätigt hat.
    _changeSeq: 0,
    _savedSeq: 0,
    // Bewusstes Verlassen (Löschen, Statuswechsel, Neu laden, eigene Formulare): keine Rückfrage
    _leavingIntentionally: false,
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
    aiPreviewTargetId: null as number | null,
    // Markierter Text, auf den sich KI-Aktionen beziehen (Anzeige im KI-Bereich)
    aiSelectionText: '',
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
    // ---- Neuer Antragseditor (Teil von #856) ----
    ansicht: config.ansicht ?? 'bisher',
    panel: '' as EditorPanel,
    // Geöffnetes Menü der Kopfzeile (datei, bearbeiten, ansicht, einfuegen, format, ablauf, weitere, stand)
    menue: '',
    dialog: '' as AblaufDialog,
    // Modus des Textes; „Vorschlagen“ (Änderungen nachverfolgen) gibt es noch nicht
    // Während der Abstimmung öffnet der neue Editor im Modus „Vorschlagen“; „Bearbeiten“ bleibt wählbar
    modus: (config.ansicht === 'neu' && ABSTIMMUNG_STATUS.includes(config.status ?? '')
      ? 'vorschlagen'
      : 'bearbeiten') as EditorModus,
    vorschlagOffen: false,
    vorschlagText: '',
    vorschlagNotiz: '',
    vorschlagSendet: false,
    speicherStoerung: '' as SpeicherStoerung,
    abstimmungAuswahlOffen: false,
    gliederung: [] as GliederungsEintrag[],
    gliederungAn: gespeicherteEinstellung(SPEICHER_GLIEDERUNG, true),
    seitenansicht: gespeicherteEinstellung(SPEICHER_SEITENANSICHT, false),
    randAntwort: '',
    randSendet: false,
    abstimmungAuswahl: [] as string[],
    abstimmungArt: 'council',
    ablaufLaeuft: false,
    ablaufFehler: '',
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
          } else if ((e.ctrlKey || e.metaKey) && e.altKey && e.key.toLowerCase() === 'm' && this.ansicht === 'neu') {
            // Strg+Alt+M = Kommentar zur markierten Stelle (wie in Google Docs)
            e.preventDefault()
            this.kommentarStarten()
          } else if (e.key === 'Escape' && this.searchOpen) {
            this.closeSearch()
          }
        }
        document.addEventListener('keydown', this._globalKeydownHandler)

        // Titel, Dokumenttyp und Briefpapier werden mitgespeichert – sie zählen als Änderung
        this.$watch('title', () => this._markChanged())

        // Rückfrage beim Verlassen mit ungespeicherten Änderungen (#185). Der Handler bleibt
        // wie der Keydown-Handler bewusst registriert: destroy() läuft auch in den
        // Recovery-Pfaden, und beim Verlassen wird die Seite ohnehin komplett neu geladen.
        window.addEventListener('beforeunload', (e: BeforeUnloadEvent) => {
          if (this._leavingIntentionally || !this.hasUnsavedChanges()) return
          e.preventDefault()
          // Ältere Browser zeigen die Rückfrage nur mit gesetztem returnValue
          e.returnValue = ''
        })
        // Eigene Formulare im Editor (z. B. „Kommentar erledigen“) verlassen die Seite
        // absichtlich; per fetch abgeschickte Formulare haben preventDefault gesetzt.
        this.$el.addEventListener('submit', (e: Event) => {
          if (!e.defaultPrevented) this.leaveIntentionally()
        })

        this.updateWordCount()

        // Speichern mit Wiederholung: wieder online bzw. wieder im Vordergrund → wartenden Versuch sofort senden
        window.addEventListener('online', () => this.speichernNachholen())
        window.addEventListener('focus', () => this.speichernNachholen())
        document.addEventListener('visibilitychange', () => {
          if (document.visibilityState === 'visible') this.speichernNachholen()
        })

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
      const scrollContainer = document.querySelector('.editor-container, .ke-scroll')
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

      // Neuer Editor: Gliederung und Rand nach dem Laden, nach Schriftladen und bei Größenänderung. Ändert sich die
      // Höhe des Textes (Bilder, Kollaboration), werden Gliederung und Rand neu berechnet.
      if (this.ansicht === 'neu') {
        this._nachAenderung()
        window.addEventListener('resize', () => this.randNeuAnordnen(), { passive: true })
        const textEl = document.querySelector<HTMLElement>('[data-ke-text]')
        if (textEl && typeof ResizeObserver !== 'undefined') {
          new ResizeObserver(() => this._nachAenderung()).observe(textEl)
        }
        void document.fonts?.ready.then(() => this.randNeuAnordnen())
        this._vorschlagStileSetzen()
        this.$watch('inlineCommentsData', () => {
          this._vorschlagStileSetzen()
          void this.$nextTick(() => this.randNeuAnordnen())
        })
        this.$watch('activeCommentId', () => this.randNeuAnordnen())
      }

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
          this._markChanged()
          this.updateWordCount()
          this._nachAenderung()
        },
        onSelectionUpdate: (state) => {
          this.updatedAt = Date.now()
          this.formats = state
          this._checkTextSelection()
          this._updateAiSelection()
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
      // Der Anfangsinhalt löst kein onUpdate aus: Gliederung und Rand nach dem Start nachführen (auch nach dem
      // Wechsel aus der Kollaboration in den Solo-Modus, bei dem die Höhe des Blatts gleich bleibt)
      this._nachAenderung()
      this._modusAnwenden()
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
            this._markChanged()
            this.updateWordCount()
            this._nachAenderung()
          },
          onSelectionUpdate: (state) => {
            this.updatedAt = Date.now()
            this.formats = state
            this._checkTextSelection()
            this._updateAiSelection()
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
          onPersisted: (contentHash) => {
            // Der Server hat unseren Kollaborationsstand gespeichert: Das ist ab jetzt
            // der Stand, von dem ein späteres Speichern ohne Verbindung ausgeht (#184).
            this.contentHash = contentHash
            this.saveConflict = false
            this._savedSeq = this._changeSeq
          },
          onReloadRequired: () => {
            // Server hat einen neuen Stand (Versions-Wiederherstellung oder Speichern
            // ohne Verbindung durch eine andere Person) → frisch laden
            showToast('Das Dokument wurde an anderer Stelle gespeichert — es wird neu geladen.', 'info')
            this.leaveIntentionally()
            window.setTimeout(() => window.location.reload(), 800)
          },
        })
        editor = result.editor
        this.collabDestroy = result.collab.destroy
        this._modusAnwenden()
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
      // Gemerkte Stellen gehören zum alten Editor und lassen sich nicht mehr übernehmen
      releaseAiTargets()
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
      // Abgebrochen: wieder die gespeicherte Sichtbarkeit (auch nach einem Vorschlag)
      this.shareVisibility = config.visibility
    },

    /**
     * Vorschlag aus dem Bezug (Issue #735): Teilen-Dialog mit der Sichtbarkeit des Bezugsantrags
     * vorausgewählt öffnen. Gespeichert wird erst mit „Speichern“ im Dialog.
     */
    suggestVisibility(visibility: string | undefined): void {
      if (visibility !== 'private' && visibility !== 'shared' && visibility !== 'organization') return
      this.shareVisibility = visibility
      this.showShareModal = true
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
      // Während ein Änderungsvorschlag geschrieben wird, gilt die Stelle von damals
      if (this.vorschlagOffen) return

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

    /** Markierten Text für den KI-Bereich nachführen (bleibt stehen, wenn der Fokus in den Chat wechselt) */
    _updateAiSelection(): void {
      if (!editor) return
      const { from, to, empty } = editor.state.selection
      this.aiSelectionText = empty ? '' : textOfRange(editor.state.doc, from, to).trim()
    },

    // === Kommentardetails (Klick auf bestehende Markierung) ===

    _handleMarkClick(markEl: HTMLElement): void {
      const markId = markEl.getAttribute('data-comment-id')
      if (!markId) return

      const comment = this.inlineCommentsData.find((c) => c.mark_id === markId)
      if (!comment) return

      // Neuer Editor: Kommentare stehen am Rand – Karte aktivieren statt Popup (am Handy bleibt das Popup)
      if (this.ansicht === 'neu' && !this.panel && window.matchMedia(RAND_AB).matches) {
        this.randKarteAktivieren(markId)
        return
      }

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
      // Antwort wird lokal ergänzt, damit das Popup sofort aktuell ist
      if (await this._kommentarAntworten(this.markCommentData, this.markReplyText.trim())) this.markReplyText = ''
      this.markReplySubmitting = false
    },

    async resolveMarkComment(): Promise<void> {
      if (!this.markCommentData) return
      // Erledigt: Markierung im Editor entfernen, Kommentarliste neu laden
      if (await this._kommentarErledigen(this.markCommentData)) this.hideMarkPopup()
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
          // Neue Markierung sofort anklickbar machen: Die Popup-Daten kommen sonst erst
          // mit dem nächsten Seitenaufbau aus dem View (#185)
          this.inlineCommentsData.push({
            id: String(data.comment.id),
            mark_id: markId,
            content: String(data.comment.content),
            selected_text: this.inlineSelectedText,
            author_name: String(data.comment.author),
            author_initials: String(data.comment.author).substring(0, 2).toUpperCase(),
            created_at: String(data.comment.created_at),
            is_resolved: false,
            replies: [],
          })
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
        const selector = '[data-kommentar-liste]'
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

    async save(force = false): Promise<void> {
      if (this.saving || !editor) return
      if (wiederholTimer) {
        window.clearTimeout(wiederholTimer)
        wiederholTimer = null
      }
      this.saving = true
      // Stand, den diese Anfrage sichert; spätere Eingaben bleiben „ungespeichert“
      const seq = this._changeSeq

      const formData = new FormData()
      // Token aus dem Cookie: Nach einer neuen Anmeldung in einem anderen Tab ist nur das Cookie aktuell
      formData.append('csrfmiddlewaretoken', csrfTokenAktuell())
      formData.append('action', 'save')
      formData.append('title', this.title)
      formData.append('content', this.getContent())
      formData.append('motion_type', this.motionType)
      formData.append('document_type_id', this.documentTypeId)
      formData.append('letterhead_id', this.letterheadId)
      // Ohne Kollaborationsverbindung den Stand mitschicken, von dem wir ausgehen:
      // Der Server überschreibt dann keinen neueren Stand still (#184). Mit
      // Verbindung führt Yjs zusammen, da braucht es die Prüfung nicht. Kam ein
      // gestörter Versuch doch an, ist die Wiederholung kein Konflikt (gleicher Inhalt).
      const verbunden = this.collabEnabled && this.collabStatus === 'connected'
      if (!verbunden && this.contentHash) formData.append('base_content_hash', this.contentHash)
      if (force) formData.append('force', '1')

      let antwort: SpeicherAntwort
      try {
        const zeitlimit = typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(ZEITLIMIT_MS) : undefined
        const response = await fetch(window.location.href, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body: formData,
          credentials: 'same-origin',
          ...(zeitlimit ? { signal: zeitlimit } : {}),
        })
        antwort = await speicherAntwort(response)
      } catch (error) {
        console.error('Save error:', error)
        antwort = { art: 'stoerung' }
      }

      if (antwort.art === 'ok') {
        this.lastSaved = new Date().toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
        this.saveError = false
        this.saveConflict = false
        this.speicherStoerung = ''
        speicherVersuche = 0
        serverfehler = 0
        this._savedSeq = seq
        if (typeof antwort.daten.content_hash === 'string') this.contentHash = antwort.daten.content_hash
      } else if (antwort.art === 'konflikt') {
        this.saveConflict = true
        this.saveError = true
        this.speicherStoerung = ''
        showToast(antwort.daten.message || 'Das Dokument wurde inzwischen an anderer Stelle geändert.', 'warning')
      } else if (
        antwort.art === 'stoerung' ||
        antwort.art === 'anmeldung' ||
        (antwort.art === 'server' && serverfehler + 1 < VERSUCHE_BEI_500)
      ) {
        // Eingabe behalten und später erneut senden (#927-Muster); jeder Versuch schickt den dann aktuellen Stand
        this.saveError = true
        speicherVersuche += 1
        if (antwort.art === 'server') serverfehler += 1
        this.speicherStoerung =
          antwort.art === 'anmeldung' ? 'anmeldung' : antwort.art === 'server' ? 'server' : 'verbindung'
        const basis =
          antwort.art === 'anmeldung'
            ? ANMELDUNG_WARTEZEIT_MS
            : WARTEZEITEN_MS[Math.min(speicherVersuche - 1, WARTEZEITEN_MS.length - 1)]
        // Etwas Streuung, damit nach einem Neustart nicht alle offenen Seiten im selben Moment senden
        const wartezeit = Math.round(basis * (0.85 + Math.random() * 0.3))
        wiederholTimer = window.setTimeout(() => {
          wiederholTimer = null
          void this.save(force)
        }, wartezeit)
      } else {
        this.saveError = true
        this.speicherStoerung = ''
        speicherVersuche = 0
        serverfehler = 0
      }

      this.saving = false
    },

    /** Wartet ein Speicherversuch (Störung, Anmeldung), jetzt senden: wieder online, Seite wieder im Vordergrund */
    speichernNachholen(): void {
      if (wiederholTimer && !this.saving) void this.save()
    },

    autoSave(): void {
      // Im Kollab-Modus persistiert der Server (yjs_save inkl. HTML +
      // gedrosselte Revisionen) — kein POST-Autosave nötig, solange
      // die Verbindung steht.
      if (this.collabEnabled && this.collabStatus === 'connected') return
      // Nach einem Konflikt nicht blind weiterversuchen: Die Person entscheidet
      // über "Neu laden" oder "Trotzdem speichern" (#184).
      if (this.saveConflict) return
      // Ohne Änderung keine Anfrage: spart Revisionen und Last (#185)
      if (!this.hasUnsavedChanges()) return
      if (!this.saving && editor) void this.save()
    },

    /** Liegen seit dem letzten erfolgreichen Speichern Änderungen vor? */
    hasUnsavedChanges(): boolean {
      return this._changeSeq !== this._savedSeq
    },

    _markChanged(): void {
      this._changeSeq += 1
    },

    /** Vor gewollter Navigation aufrufen: Die Rückfrage gilt nur für unbeabsichtigtes Verlassen. */
    leaveIntentionally(): void {
      this._leavingIntentionally = true
    },

    /** Konflikt (#184): den eigenen Stand bewusst über den neueren schreiben. */
    saveOverwrite(): Promise<void> {
      return this.save(true)
    },

    /** Konflikt (#184): den neueren Stand vom Server holen; eigene Änderungen gehen verloren. */
    reloadFromServer(): void {
      // Die Person verwirft ihre Änderungen bewusst – keine zusätzliche Rückfrage
      this.leaveIntentionally()
      window.location.reload()
    },

    setDocumentType(id: string, name: string): void {
      this.documentTypeId = id
      this.documentTypeName = name
      this._markChanged()
    },

    setLetterhead(id: string, name: string): void {
      this.letterheadId = id
      this.letterheadName = name
      this._markChanged()

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
      // Nach dem Wechsel lädt die Seite neu: Ohne Kollaborationsverbindung vorher speichern, sonst gingen
      // Eingaben seit dem letzten automatischen Speichern verloren (mit Verbindung sichert Yjs laufend)
      if (!(await this._vorNeuladenSichern())) return
      try {
        const response = await fetch(config.urls.status, {
          method: 'POST',
          headers: formHeaders(),
          body: new URLSearchParams({ status: newStatus }),
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          this.leaveIntentionally()
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
          this.leaveIntentionally()
          navigateTo(config.urls.documents)
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
      this.chatMessages.push({ role: 'user', content: escapeHtml(msg) })
      this.userMessage = ''
      void this.aiAction('chat', msg)
    },

    async aiAction(action: string, instruction = ''): Promise<void> {
      this.aiLoading = true
      void this.$nextTick(() => {
        const container = this.$refs.chatMessages
        if (container) container.scrollTop = container.scrollHeight
      })

      // Vorschläge (alles außer dem Chat) gelten für die markierte Stelle: Nur sie geht an die KI, und nur
      // sie wird beim Übernehmen ersetzt. Ohne Markierung liest die KI den ganzen Text wie bisher, ihr
      // Vorschlag lässt sich dann aber nicht übernehmen (nie das ganze Dokument ersetzen, Teil von #856).
      const isSuggestion = action !== 'chat'
      const target = editor ? captureAiTarget(editor) : null
      const text = isSuggestion && target ? target.text : editor ? this.getContent() : ''
      const selectedText = target ? target.text : ''
      let targetUsed = false
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
          let targetId: number | null = null
          let hint = ''

          if (data.content) {
            content = bereinigeKiHtml(String(data.content).replace(/\n/g, '<br>'))
            actionContent = data.content
            if (isSuggestion && target) {
              hasAction = true
              targetId = ++aiTargetSeq
              aiTargets.set(targetId, target)
              targetUsed = true
            } else if (isSuggestion) {
              hint = 'Zum Übernehmen markieren Sie die Stelle im Text und wählen die Aktion erneut.'
            }
          }
          if (Array.isArray(data.suggestions) && data.suggestions.length > 0) {
            content += '<ul class="mt-2 space-y-1">'
            for (const s of data.suggestions) {
              content += `<li class="text-sm">${escapeHtml(String(s))}</li>`
            }
            content += '</ul>'
          }
          if (!content) content = 'Keine Vorschlaege verfuegbar.'

          this.chatMessages.push({ role: 'ai', content, hasAction, actionContent, targetId, hint })
        } else {
          this.chatMessages.push({
            role: 'ai',
            content:
              'Entschuldigung, es ist ein Fehler aufgetreten: ' +
              escapeHtml(String(data.error || 'Unbekannter Fehler')),
          })
        }
      } catch {
        this.chatMessages.push({
          role: 'ai',
          content: 'Entschuldigung, die Verbindung zum KI-Service ist fehlgeschlagen.',
        })
      }

      // Stelle ohne übernehmbaren Vorschlag (Fehler, Chat, leere Antwort) nicht weiter verfolgen
      if (target && !targetUsed) target.release()

      this.aiLoading = false
      void this.$nextTick(() => {
        const container = this.$refs.chatMessages
        if (container) container.scrollTop = container.scrollHeight
      })
    },

    applyAiContent(content: string | null | undefined, targetId: number | null = null): void {
      const target = targetId === null ? undefined : aiTargets.get(targetId)
      if (!content || !editor || !target) {
        showToast(
          'Dieser Vorschlag lässt sich nicht mehr übernehmen. Markieren Sie die Stelle und fragen Sie erneut.',
          'warning',
        )
        return
      }
      // Original ist nur die markierte Stelle (Text), nicht das ganze Dokument
      this.aiOriginalContent = escapeHtml(target.text).replace(/\n/g, '<br>')
      // Vorschau per x-html: nur bereinigt (frontend/js/ki-ausgabe.ts)
      this.aiPreviewContent = bereinigeKiHtml(content)
      this.aiPreviewTargetId = targetId
      this.previewMode = 'suggested'
      this.aiPreviewActive = true
    },

    closeAiPreview(): void {
      this.aiPreviewActive = false
      this.aiPreviewContent = ''
      this.aiOriginalContent = ''
      this.aiPreviewTargetId = null
      this.previewMode = 'suggested'
    },

    /** Vorschlag verwerfen: Nachricht entfernen, Stelle nicht weiter verfolgen */
    discardAiMessage(index: number): void {
      const message = this.chatMessages[index]
      if (!message) return
      if (message.targetId != null) {
        aiTargets.get(message.targetId)?.release()
        aiTargets.delete(message.targetId)
      }
      this.chatMessages.splice(index, 1)
    },

    /**
     * Vorschlag nur an der markierten Stelle einsetzen (nie das ganze Dokument). Hat jemand die Stelle
     * inzwischen geändert, bleibt alles, wie es ist, und der Vorschlag lässt sich neu anfragen.
     */
    applyAiPreview(): void {
      const targetId = this.aiPreviewTargetId
      const target = targetId === null ? undefined : aiTargets.get(targetId)
      if (!this.aiPreviewContent || !editor || !target || targetId === null) {
        this.closeAiPreview()
        return
      }
      let result: ReturnType<typeof applyAiSuggestion> = 'failed'
      try {
        result = applyAiSuggestion(editor, target, this.aiPreviewContent)
      } catch (error) {
        console.error('KI-Vorschlag konnte nicht eingesetzt werden:', error)
      }
      if (result === 'applied') {
        aiTargets.delete(targetId)
        for (const message of this.chatMessages) {
          if (message.targetId === targetId) message.hasAction = false
        }
        this.chatMessages.push({ role: 'ai', content: 'Der Vorschlag wurde an der markierten Stelle übernommen.' })
      } else {
        const text =
          result === 'changed'
            ? 'Die markierte Stelle wurde inzwischen geändert. Der Vorschlag wurde nicht übernommen.'
            : 'Der Vorschlag ließ sich nicht einsetzen, ohne anderen Text zu ändern. Es wurde nichts übernommen.'
        showToast(text, 'warning')
        this.chatMessages.push({ role: 'ai', content: text })
      }
      this.closeAiPreview()
    },

    /** Ungespeicherte Änderungen vor einem Neuladen sichern (ohne Kollaborationsverbindung); false = abbrechen */
    async _vorNeuladenSichern(): Promise<boolean> {
      const verbunden = this.collabEnabled && this.collabStatus === 'connected'
      if (verbunden || !editor || !this.hasUnsavedChanges()) return true
      await this.save()
      if (this.saveError || this.hasUnsavedChanges()) {
        showToast('Ihre Änderungen ließen sich nicht speichern. Der Schritt wurde nicht ausgeführt.', 'error')
        return false
      }
      return true
    },

    // === Neuer Antragseditor (Teil von #856): Menüs, Rand, Gliederung, Ablauf ===

    /** Offene Inline-Kommentare für den Rand */
    get randKarten(): InlineComment[] {
      return this.inlineCommentsData.filter((c) => !c.is_resolved)
    },

    menueUmschalten(key: string): void {
      this.menue = this.menue === key ? '' : key
    },

    menueSchliessen(): void {
      this.menue = ''
    },

    /** Pfeiltasten in der Menüleiste und in geöffneten Menüs (Fokus wandert, Escape schließt) */
    menueTaste(event: KeyboardEvent): void {
      const target = event.target as HTMLElement | null
      if (!target) return
      if (event.key === 'Escape' && this.menue) {
        const knopf = document.querySelector<HTMLElement>(`[data-menue-knopf="${this.menue}"]`)
        this.menueSchliessen()
        knopf?.focus()
        event.preventDefault()
        return
      }
      const liste = target.closest<HTMLElement>('[role="menu"], [role="menubar"]')
      if (!liste || !['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
      const punkte = Array.from(
        liste.querySelectorAll<HTMLElement>(':scope > [role^="menuitem"], :scope > * > [role^="menuitem"]'),
      ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null)
      if (!punkte.length) return
      const quer = liste.getAttribute('role') === 'menubar'
      const vor = quer ? 'ArrowRight' : 'ArrowDown'
      const zurueck = quer ? 'ArrowLeft' : 'ArrowUp'
      const index = punkte.indexOf(target)
      let ziel = index
      if (event.key === vor) ziel = (index + 1) % punkte.length
      else if (event.key === zurueck) ziel = (index - 1 + punkte.length) % punkte.length
      else if (event.key === 'Home') ziel = 0
      else if (event.key === 'End') ziel = punkte.length - 1
      else return
      event.preventDefault()
      punkte[ziel].focus()
      // In der Menüleiste folgt ein offenes Menü dem Fokus
      if (quer && this.menue) this.menue = punkte[ziel].dataset.menueKnopf ?? ''
    },

    /** Formatbefehl aus Menü oder Werkzeugleiste (Tastatur und Maus; der Editor behält seine Auswahl) */
    befehl(cmd: string, level = 0, value = ''): void {
      this._executeToolbarCommand(cmd, level, value)
      this.menueSchliessen()
    },

    /** Absatzformat aus der Auswahlliste der Werkzeugleiste (p, h1, h2, h3, quote) */
    absatzformat(wert: string): void {
      if (wert === 'quote') this.befehl('blockquote')
      else if (wert.startsWith('h')) this.befehl('heading', Number(wert.slice(1)))
      else this.befehl('paragraph')
    },

    get absatzformatWert(): string {
      if (this.formats.header) return `h${this.formats.header}`
      if (this.formats.blockquote) return 'quote'
      return 'p'
    },

    panelOeffnen(key: EditorPanel): void {
      this.menueSchliessen()
      this.panel = this.panel === key ? '' : key
      if (key === 'verlauf' && this.panel && this.revisions.length === 0) void this.loadRevisions()
      void this.$nextTick(() => {
        if (!this.panel) this.randNeuAnordnen()
        const kopf = document.querySelector<HTMLElement>('[data-panel-titel]')
        if (this.panel && kopf) kopf.focus()
      })
    },

    panelSchliessen(): void {
      this.panel = ''
      void this.$nextTick(() => this.randNeuAnordnen())
    },

    /** Kommentar zur markierten Stelle (Werkzeugleiste, Menü „Einfügen“, Strg+Alt+M) */
    kommentarStarten(): void {
      this.menueSchliessen()
      if (!editor || editor.state.selection.empty) {
        showToast('Markieren Sie zuerst die Textstelle, die Sie kommentieren möchten.', 'info')
        return
      }
      this._checkTextSelection()
      this.expandCommentPopup()
    },

    modusSetzen(modus: EditorModus): void {
      this.menueSchliessen()
      this.modus = modus
      if (modus !== 'vorschlagen') this.vorschlagAbbrechen()
      this._modusAnwenden()
    },

    /** Nur „Bearbeiten“ ändert den Text direkt; „Vorschlagen“ und „Ansehen“ lassen markieren, aber nicht tippen */
    _modusAnwenden(): void {
      // Ohne Update-Ereignis: Der Moduswechsel ist keine Änderung am Text (sonst gälte der Text als ungespeichert
      // und würde beim nächsten Speichern neu geschrieben)
      if (editor && !editor.isDestroyed && editor.isEditable !== (this.modus === 'bearbeiten')) {
        editor.setEditable(this.modus === 'bearbeiten', false)
      }
    },

    /** Änderung zur markierten Stelle vorschlagen (Werkzeugleiste, Menü „Einfügen“, Knopf an der Auswahl) */
    vorschlagStarten(): void {
      this.menueSchliessen()
      if (!editor || editor.state.selection.empty) {
        showToast('Markieren Sie zuerst die Textstelle, die Sie ändern möchten.', 'info')
        return
      }
      const { from, to } = editor.state.selection
      if (!inEinemAbsatz(editor.state.doc, from, to)) {
        showToast('Ein Vorschlag gilt für eine Stelle innerhalb eines Absatzes. Markieren Sie weniger Text.', 'info')
        return
      }
      this._checkTextSelection()
      if (!this.inlineSelectedText) return
      this.commentPopupVisible = false
      this.vorschlagText = this.inlineSelectedText
      this.vorschlagNotiz = ''
      this.vorschlagOffen = true
      void this.$nextTick(() => {
        const feld = this.$refs.vorschlagFeld as HTMLTextAreaElement | undefined
        feld?.focus()
        feld?.select()
      })
    },

    /** Markierte Stelle im Formular „Änderung vorschlagen“ (gekürzt) */
    get vorschlagZitat(): string {
      const t = this.inlineSelectedText
      return t.length > 120 ? `${t.slice(0, 119)}…` : t
    },

    vorschlagAbbrechen(): void {
      this.vorschlagOffen = false
      this.vorschlagText = ''
      this.vorschlagNotiz = ''
    },

    /** Vorschlag als Kommentar an der Stelle speichern; der Text selbst bleibt bis zur Entscheidung unverändert */
    async vorschlagSenden(): Promise<void> {
      if (this.vorschlagSendet || !editor || !config.urls.comment) return
      const alt = this.inlineSelectedText
      // Ein Vorschlag gilt innerhalb eines Absatzes: Zeilenumbrüche werden zu Leerzeichen
      const neu = this.vorschlagText.replace(/\s*\n\s*/g, ' ')
      if (neu === alt) {
        showToast('Der Vorschlag ändert nichts an der markierten Stelle.', 'info')
        return
      }
      this.vorschlagSendet = true
      // Marke SYNCHRON setzen, bevor sich Positionen ändern (wie bei Kommentaren)
      const markId = crypto.randomUUID()
      let markGesetzt = false
      try {
        editor
          .chain()
          .setTextSelection({ from: this._selectionFrom, to: this._selectionTo })
          .setCommentMark({ commentId: markId })
          .run()
        markGesetzt = true
      } catch (e) {
        console.warn('Vorschlag: Marke ließ sich nicht setzen', e)
      }
      const notiz = this.vorschlagNotiz.trim()
      const daten = new URLSearchParams({
        content: notiz,
        vorschlag: neu,
        selected_text: alt,
        selection_start: String(this._selectionFrom),
        selection_end: String(this._selectionTo),
        mark_id: markId,
      })
      const zuruecknehmen = (): void => {
        if (markGesetzt && editor) editor.commands.unsetCommentMark(markId)
      }
      try {
        const response = await fetch(config.urls.comment, { method: 'POST', headers: formHeaders(), body: daten })
        const antwort: JsonResponse = await response.json().catch(() => ({}))
        if (antwort.success && antwort.comment) {
          this.inlineCommentsData.push({
            id: String(antwort.comment.id),
            author_id: config.membershipId ?? '',
            mark_id: markId,
            content: String(antwort.comment.content),
            selected_text: alt,
            author_name: String(antwort.comment.author),
            author_initials: String(antwort.comment.author).substring(0, 2).toUpperCase(),
            created_at: String(antwort.comment.created_at),
            is_resolved: false,
            replies: [],
            vorschlag: neu,
            vorschlag_angenommen: null,
            notiz,
          })
          this.vorschlagAbbrechen()
          this.activeCommentId = markId
          await this.reloadCommentsSidebar()
        } else {
          zuruecknehmen()
          showToast(
            typeof antwort.error === 'string' ? antwort.error : 'Der Vorschlag ließ sich nicht speichern.',
            'error',
          )
        }
      } catch {
        zuruecknehmen()
        showToast('Der Vorschlag ließ sich nicht speichern. Bitte versuchen Sie es erneut.', 'error')
      }
      this.vorschlagSendet = false
    },

    istVorschlag(comment: InlineComment): boolean {
      return comment.vorschlag !== null && comment.vorschlag !== undefined
    },

    /** Annehmen ändert den Text: nur mit Schreibrecht im Editor */
    darfAnnehmen(): boolean {
      return this.accessLevel === 'edit' || this.accessLevel === 'admin'
    },

    /** Ablehnen: wer annehmen darf, wer den Vorschlag gemacht hat oder wer alle Dokumente bearbeiten darf */
    darfAblehnen(comment: InlineComment): boolean {
      return this.darfAnnehmen() || this.darfErledigen(comment)
    },

    /**
     * Vorschlag annehmen oder ablehnen. Annehmen prüft zuerst, ob die Stelle noch den markierten Text trägt,
     * speichert dann die Entscheidung und ersetzt die Stelle. Hat sich die Stelle geändert, wird nichts ersetzt.
     */
    async vorschlagEntscheiden(comment: InlineComment, annehmen: boolean): Promise<void> {
      if (this.randSendet) return
      if (annehmen && (!editor || !vorschlagPasst(editor, comment.mark_id, comment.selected_text))) {
        showToast(
          'Die Stelle wurde inzwischen geändert. Der Vorschlag lässt sich nicht mehr übernehmen; lehnen Sie ihn ab oder ändern Sie den Text von Hand.',
          'warning',
        )
        return
      }
      this.randSendet = true
      try {
        const response = await fetch(`${config.urls.comment}${comment.id}/resolve/`, {
          method: 'POST',
          headers: formHeaders(),
          body: new URLSearchParams({ entscheidung: annehmen ? 'annehmen' : 'ablehnen' }),
        })
        const antwort: JsonResponse = await response.json().catch(() => ({}))
        if (!antwort.success) {
          showToast('Die Entscheidung ließ sich nicht speichern.', 'error')
          return
        }
        comment.is_resolved = true
        comment.vorschlag_angenommen = annehmen
        if (editor) {
          const ergebnis = annehmen
            ? vorschlagUebernehmen(editor, comment.mark_id, comment.selected_text, comment.vorschlag ?? '')
            : 'abgelehnt'
          if (ergebnis !== 'applied') editor.commands.unsetCommentMark(comment.mark_id)
          if (ergebnis === 'changed') {
            showToast(
              'Angenommen, aber die Stelle hatte sich gerade geändert. Bitte übernehmen Sie den Text von Hand.',
              'warning',
            )
          }
          // Geänderten Text gleich sichern (mit Kollaborationsverbindung speichert der Server)
          if (ergebnis === 'applied') this.autoSave()
        }
        if (this.activeCommentId === comment.mark_id) this.activeCommentId = null
        await this.reloadCommentsSidebar()
      } catch {
        showToast('Die Entscheidung ließ sich nicht speichern.', 'error')
      } finally {
        this.randSendet = false
        this.randNeuAnordnen()
      }
    },

    /** Offene Vorschläge im Text grün statt gelb hervorheben (Regeln je Marken-ID) */
    _vorschlagStileSetzen(): void {
      if (typeof CSSStyleSheet === 'undefined' || !('adoptedStyleSheets' in document)) return
      const ids = this.inlineCommentsData
        .filter((c) => !c.is_resolved && this.istVorschlag(c))
        .map((c) => CSS.escape(c.mark_id))
      if (!vorschlagStil) {
        if (!ids.length) return
        vorschlagStil = new CSSStyleSheet()
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, vorschlagStil]
      }
      // Mit #editor-container: Die Grundstile der Kommentarmarken hängen an dieser ID (sonst gewinnen sie)
      const auswahl = ids
        .map(
          (id) =>
            `.ke-editor [data-ke-text] span[data-comment-id="${id}"],.ke-editor #editor-container .tiptap span[data-comment-id="${id}"]`,
        )
        .join(',')
      vorschlagStil.replaceSync(
        auswahl
          ? `${auswahl}{background-color:var(--ke-vorschlag) !important;border-bottom:1.5px dashed var(--ke-gruen) !important}`
          : '',
      )
    },

    /** „Ersetzen: „alt“ durch „neu““ bzw. „Streichen: „alt““ für die Karte am Rand */
    vorschlagZeile(comment: InlineComment): string {
      const kurz = (t: string): string => (t.length > 160 ? `${t.slice(0, 159)}…` : t)
      if (!comment.vorschlag) return `Streichen: „${kurz(comment.selected_text)}“`
      return `Ersetzen: „${kurz(comment.selected_text)}“ durch „${kurz(comment.vorschlag)}“`
    },

    gliederungUmschalten(): void {
      this.gliederungAn = !this.gliederungAn
      einstellungMerken(SPEICHER_GLIEDERUNG, this.gliederungAn)
      this.menueSchliessen()
      void this.$nextTick(() => this.randNeuAnordnen())
    },

    seitenansichtUmschalten(): void {
      this.seitenansicht = !this.seitenansicht
      einstellungMerken(SPEICHER_SEITENANSICHT, this.seitenansicht)
      this.menueSchliessen()
      void this.$nextTick(() => this.randNeuAnordnen())
    },

    zuUeberschrift(index: number): void {
      const text = document.querySelector<HTMLElement>('[data-ke-text]')
      if (text) zuUeberschriftSpringen(text, index)
    },

    /** Klick im Editor: offene Menüs schließen, außer der Klick gilt einem Menü oder seinem Knopf */
    menueKlick(event: MouseEvent): void {
      if (!this.menue) return
      const ziel = event.target as Element | null
      if (ziel?.closest('[data-menue-knopf], [role="menu"]')) return
      this.menueSchliessen()
    },

    /** Kommentar erledigen dürfen Verfasserin bzw. Verfasser und wer alle Dokumente bearbeiten darf */
    darfErledigen(comment: InlineComment): boolean {
      return !!config.erledigenAlle || (!!comment.author_id && comment.author_id === config.membershipId)
    },

    /** Nach jeder Änderung (gebündelt): Gliederung und Kommentare am Rand nachführen */
    _nachAenderung(): void {
      if (this.ansicht !== 'neu') return
      if (nachAenderungTimer) window.clearTimeout(nachAenderungTimer)
      nachAenderungTimer = window.setTimeout(() => {
        nachAenderungTimer = null
        const text = document.querySelector<HTMLElement>('[data-ke-text]')
        if (text) this.gliederung = gliederungAus(text)
        this.randNeuAnordnen()
      }, 150)
    },

    /** Karten am Rand neben ihre Textstelle setzen (nur Desktop, nur wenn der Rand sichtbar ist) */
    randNeuAnordnen(): void {
      if (this.ansicht !== 'neu') return
      if (randFrame) window.cancelAnimationFrame(randFrame)
      randFrame = window.requestAnimationFrame(() => {
        randFrame = null
        const rand = document.querySelector<HTMLElement>('[data-rand]')
        const text = document.querySelector<HTMLElement>('[data-ke-text]')
        if (!rand || !text) return
        if (this.panel || !window.matchMedia(RAND_AB).matches) {
          rand.style.minHeight = ''
          return
        }
        const hoehe = randAnordnen(rand, text, this.activeCommentId)
        rand.style.minHeight = hoehe ? `${hoehe + 24}px` : ''
      })
    },

    randKarteAktivieren(markId: string): void {
      if (this.activeCommentId !== markId) this.randAntwort = ''
      this.activeCommentId = markId
      this.randNeuAnordnen()
    },

    async randAntworten(comment: InlineComment): Promise<void> {
      const text = this.randAntwort.trim()
      if (!text || this.randSendet) return
      this.randSendet = true
      if (await this._kommentarAntworten(comment, text)) this.randAntwort = ''
      this.randSendet = false
      this.randNeuAnordnen()
    },

    async randErledigen(comment: InlineComment): Promise<void> {
      if (await this._kommentarErledigen(comment)) {
        if (this.activeCommentId === comment.mark_id) this.activeCommentId = null
        this.randNeuAnordnen()
      }
    },

    /** Antwort auf einen Inline-Kommentar speichern und lokal ergänzen */
    async _kommentarAntworten(comment: InlineComment, text: string): Promise<boolean> {
      const formData = new URLSearchParams()
      formData.append('csrfmiddlewaretoken', csrfToken())
      formData.append('content', text)
      formData.append('parent', comment.id)
      try {
        const response = await fetch(config.urls.comment, { method: 'POST', headers: formHeaders(), body: formData })
        const data: JsonResponse = await response.json()
        if (data.success && data.comment) {
          comment.replies.push({
            id: data.comment.id,
            content: data.comment.content,
            author_name: data.comment.author,
            author_initials: String(data.comment.author).substring(0, 2).toUpperCase(),
            created_at: data.comment.created_at,
          })
          return true
        }
      } catch (error) {
        console.error('Reply error:', error)
      }
      showToast('Fehler beim Speichern der Antwort.', 'error')
      return false
    },

    /** Inline-Kommentar erledigen: Server, Markierung im Text, Kommentarliste */
    async _kommentarErledigen(comment: InlineComment): Promise<boolean> {
      try {
        const response = await fetch(`${config.urls.comment}${comment.id}/resolve/`, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken() },
        })
        const data: JsonResponse = await response.json()
        if (data.success) {
          comment.is_resolved = true
          if (editor) editor.commands.unsetCommentMark(comment.mark_id)
          await this.reloadCommentsSidebar()
          return true
        }
      } catch {
        // Meldung unten
      }
      showToast('Fehler beim Erledigen.', 'error')
      return false
    },

    /** Dialog eines Ablaufschritts öffnen (Kopfzeile, Infozeile, Menü „Ablauf“) */
    ablaufDialog(key: AblaufDialog): void {
      this.menueSchliessen()
      this.ablaufFehler = ''
      if (key === 'abstimmung' && !this.abstimmungAuswahl.length) {
        this.abstimmungAuswahl = readJsonScript<string[]>(ABSTIMMUNG_ID) ?? []
        // Ohne Stimmberechtigte (Recht „Stimmrecht“ nicht vergeben) gleich die Auswahl zeigen
        this.abstimmungAuswahlOffen = !this.abstimmungAuswahl.length
      }
      this.dialog = key
    },

    ablaufDialogSchliessen(): void {
      if (!this.ablaufLaeuft) this.dialog = ''
    },

    /** „Wer stimmt ab“: die Stimmberechtigten (Vorauswahl) oder die geänderte Auswahl */
    get abstimmungWer(): string {
      const vorauswahl = readJsonScript<string[]>(ABSTIMMUNG_ID) ?? []
      const anzahl = this.abstimmungAuswahl.length
      const gleich = anzahl === vorauswahl.length && vorauswahl.every((id) => this.abstimmungAuswahl.includes(id))
      if (!anzahl) return 'Noch niemand ausgewählt'
      if (gleich) return anzahl === 1 ? '1 stimmberechtigtes Mitglied' : `${anzahl} stimmberechtigte Mitglieder`
      return anzahl === 1 ? '1 Mitglied ausgewählt' : `${anzahl} Mitglieder ausgewählt`
    },

    /**
     * „Zur Abstimmung geben“: Status „Interne Absprache“ (falls noch nicht) und je ausgewähltem Mitglied eine
     * Zustimmungsanfrage über den vorhandenen Freigabe-Weg; danach lädt die Seite neu.
     */
    async abstimmungStarten(): Promise<void> {
      if (this.ablaufLaeuft || !config.urls.approvalRequest) return
      if (!this.abstimmungAuswahl.length) {
        this.ablaufFehler = 'Wählen Sie mindestens ein Mitglied aus.'
        return
      }
      if (!(await this._vorNeuladenSichern())) return
      this.ablaufLaeuft = true
      this.ablaufFehler = ''
      try {
        if (config.status !== 'internal_review' && config.status !== 'external_review') {
          const response = await fetch(config.urls.status, {
            method: 'POST',
            headers: formHeaders(),
            body: new URLSearchParams({ status: 'internal_review' }),
          })
          const data: JsonResponse = await response.json()
          if (!data.success) {
            this.ablaufFehler = data.error || 'Die Abstimmung ließ sich nicht starten.'
            return
          }
        }
        let fehler = 0
        for (const approver of this.abstimmungAuswahl) {
          const response = await fetch(config.urls.approvalRequest, {
            method: 'POST',
            headers: formHeaders(),
            body: new URLSearchParams({ approver, approval_type: this.abstimmungArt }),
          })
          const data: JsonResponse = await response.json().catch(() => ({}))
          if (!response.ok || !data.success) fehler += 1
        }
        if (fehler) {
          showToast(
            fehler === 1
              ? 'Eine Anfrage ließ sich nicht stellen; sie fehlt in der Abstimmung.'
              : `${fehler} Anfragen ließen sich nicht stellen; sie fehlen in der Abstimmung.`,
            'warning',
          )
        }
        this.leaveIntentionally()
        window.setTimeout(() => location.reload(), fehler ? 1500 : 0)
      } catch {
        this.ablaufFehler = 'Die Abstimmung ließ sich nicht starten. Bitte versuchen Sie es erneut.'
      } finally {
        this.ablaufLaeuft = false
      }
    },

    async ablaufStatus(status: string): Promise<void> {
      this.ablaufLaeuft = true
      await this.changeStatus(status)
      this.ablaufLaeuft = false
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
