/**
 * Real-time collaboration module for the Mandari Document Editor.
 *
 * Uses Yjs for CRDT-based conflict-free editing with a custom WebSocket
 * transport that communicates with the Django Channels consumer.
 */

import * as Y from 'yjs'
import * as awarenessProtocol from 'y-protocols/awareness'
import * as syncProtocol from 'y-protocols/sync'
import * as encoding from 'lib0/encoding'
import * as decoding from 'lib0/decoding'
import Collaboration from '@tiptap/extension-collaboration'
import CollaborationCursor from '@tiptap/extension-collaboration-cursor'
import { IndexeddbPersistence } from 'y-indexeddb'

export interface CollabOptions {
  /** WebSocket URL, e.g. ws://localhost:8000/ws/documents/<id>/ */
  wsUrl: string
  /** Document ID for IndexedDB persistence */
  documentId?: string
  /** Current user info */
  user: {
    name: string
    color: string
  }
  /** Called when presence list changes */
  onPresenceChange?: (users: CollabUser[]) => void
  /** Called on connection status change */
  onStatusChange?: (status: 'connecting' | 'connected' | 'disconnected') => void
  /**
   * Called when the server sends the initial Yjs state.
   * `hasState` is true if the server had a saved Yjs state, false if empty.
   * The editor should only seed from HTML content when hasState is false.
   */
  onInitialState?: (hasState: boolean) => void
  /**
   * Returns the current editor HTML. When provided, the HTML is sent along
   * with each yjs_save so the server can keep content_encrypted current and
   * create throttled revisions.
   */
  getHtml?: () => string
  /**
   * Called when the server requests a document reload (e.g. after a revision
   * restore). Default behavior: window.location.reload().
   */
  onReloadRequired?: () => void
}

export interface CollabUser {
  name: string
  color: string
  clientId?: number
}

export interface CollabResult {
  /** Yjs document instance */
  ydoc: Y.Doc
  /** TipTap extensions to add to the editor */
  extensions: any[]
  /** Awareness protocol instance */
  awareness: awarenessProtocol.Awareness
  /** Undo manager for collaborative history */
  undoManager: Y.UndoManager
  /** Destroy collaboration (call on editor destroy) */
  destroy: () => void
}

// Message types matching the Django consumer protocol
const MSG_SYNC = 0
const MSG_AWARENESS = 1

/**
 * Custom WebSocket provider for Django Channels + Yjs.
 *
 * Instead of using y-websocket's built-in provider (which expects a
 * y-websocket compatible server), this sends binary Yjs sync messages
 * encoded as base64 JSON to the Django consumer.
 */
class DjangoYjsProvider {
  private ws: WebSocket | null = null
  private ydoc: Y.Doc
  private awareness: awarenessProtocol.Awareness
  private wsUrl: string
  private connected = false
  private reconnectTimer: any = null
  private reconnectAttempts = 0
  private saveTimer: any = null
  private destroyed = false
  private initialStateReceived = false
  private onStatusChange?: (status: string) => void
  private onInitialState?: (hasState: boolean) => void
  private getHtml?: () => string
  private onReloadRequired?: () => void
  private _beforeUnloadHandler: (() => void) | null = null
  private _visibilityHandler: (() => void) | null = null

  constructor(
    wsUrl: string,
    ydoc: Y.Doc,
    awareness: awarenessProtocol.Awareness,
    onStatusChange?: (status: string) => void,
    onInitialState?: (hasState: boolean) => void,
    getHtml?: () => string,
    onReloadRequired?: () => void
  ) {
    this.wsUrl = wsUrl
    this.ydoc = ydoc
    this.awareness = awareness
    this.onStatusChange = onStatusChange
    this.onInitialState = onInitialState
    this.getHtml = getHtml
    this.onReloadRequired = onReloadRequired

    // Listen to Yjs document updates
    this.ydoc.on('update', this._onDocUpdate)

    // Listen to awareness updates
    this.awareness.on('update', this._onAwarenessUpdate)

    // Periodically save full Yjs state to server (every 10s)
    this.saveTimer = setInterval(() => this._saveFullState(), 10000)

    // Save aggressively when browser/tab lifecycle changes.
    this._beforeUnloadHandler = () => this._saveFullState()
    this._visibilityHandler = () => {
      if (document.visibilityState === 'hidden') {
        this._saveFullState()
      }
    }
    window.addEventListener('beforeunload', this._beforeUnloadHandler)
    document.addEventListener('visibilitychange', this._visibilityHandler)

    this.connect()
  }

  connect() {
    if (this.destroyed) return
    this.onStatusChange?.('connecting')

    try {
      this.ws = new WebSocket(this.wsUrl)
      this.ws.binaryType = 'arraybuffer'

      this.ws.onopen = () => {
        this.connected = true
        this.reconnectAttempts = 0
        this.onStatusChange?.('connected')

        // Send initial sync step 1
        const encoder = encoding.createEncoder()
        encoding.writeVarUint(encoder, MSG_SYNC)
        syncProtocol.writeSyncStep1(encoder, this.ydoc)
        this._sendBinary(encoding.toUint8Array(encoder))

        // Send awareness state
        const awarenessUpdate = awarenessProtocol.encodeAwarenessUpdate(
          this.awareness,
          [this.ydoc.clientID]
        )
        const aEncoder = encoding.createEncoder()
        encoding.writeVarUint(aEncoder, MSG_AWARENESS)
        encoding.writeVarUint8Array(aEncoder, awarenessUpdate)
        this._sendBinary(encoding.toUint8Array(aEncoder))
      }

      this.ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          // JSON message from Django consumer
          this._handleJsonMessage(JSON.parse(event.data))
        } else {
          // Binary message (Yjs protocol)
          this._handleBinaryMessage(new Uint8Array(event.data))
        }
      }

      this.ws.onclose = () => {
        this.connected = false
        this.onStatusChange?.('disconnected')
        this._scheduleReconnect()
      }

      this.ws.onerror = () => {
        // onclose will also fire
      }
    } catch (e) {
      this.onStatusChange?.('disconnected')
      this._scheduleReconnect()
    }
  }

  private _handleJsonMessage(msg: any) {
    if (msg.type === 'yjs_sync' && msg.data) {
      // Received Yjs sync data as base64
      const data = this._b64ToUint8(msg.data)
      this._handleBinaryMessage(data)
    } else if (msg.type === 'awareness' && msg.data) {
      try {
        const data = this._b64ToUint8(msg.data)
        awarenessProtocol.applyAwarenessUpdate(this.awareness, data, this)
      } catch (e) {
        console.warn('Failed to apply awareness update:', e)
      }
    } else if (msg.type === 'yjs_state') {
      // Initial state from server. data is base64 string or null.
      if (!this.initialStateReceived) {
        this.initialStateReceived = true
        if (msg.data) {
          try {
            const state = this._b64ToUint8(msg.data)
            Y.applyUpdate(this.ydoc, state)
            this.onInitialState?.(true)
          } catch (e) {
            console.warn('Failed to apply Yjs state from server, starting fresh:', e)
            this.onInitialState?.(false)
          }
        } else {
          // Server has no saved state — client should seed from HTML
          this.onInitialState?.(false)
        }
      }
    } else if (msg.type === 'reload') {
      // Server requests a full document reload (e.g. after revision restore).
      if (this.onReloadRequired) {
        this.onReloadRequired()
      } else {
        window.location.reload()
      }
    }
  }

  private _handleBinaryMessage(data: Uint8Array) {
    try {
      const decoder = decoding.createDecoder(data)
      const msgType = decoding.readVarUint(decoder)

      if (msgType === MSG_SYNC) {
        const encoder = encoding.createEncoder()
        encoding.writeVarUint(encoder, MSG_SYNC)
        syncProtocol.readSyncMessage(
          decoder,
          encoder,
          this.ydoc,
          this
        )
        if (encoding.length(encoder) > 1) {
          this._sendBinary(encoding.toUint8Array(encoder))
        }
      } else if (msgType === MSG_AWARENESS) {
        const update = decoding.readVarUint8Array(decoder)
        awarenessProtocol.applyAwarenessUpdate(this.awareness, update, this)
      }
    } catch (e) {
      console.warn('Failed to handle binary message:', e)
    }
  }

  private _onDocUpdate = (update: Uint8Array, origin: any) => {
    if (origin === this) return // Don't echo our own updates
    const encoder = encoding.createEncoder()
    encoding.writeVarUint(encoder, MSG_SYNC)
    syncProtocol.writeUpdate(encoder, update)
    this._sendBinary(encoding.toUint8Array(encoder))
  }

  private _onAwarenessUpdate = (
    { added, updated, removed }: { added: number[]; updated: number[]; removed: number[] },
    origin: any
  ) => {
    if (origin === this) return
    const changedClients = added.concat(updated).concat(removed)
    const update = awarenessProtocol.encodeAwarenessUpdate(this.awareness, changedClients)
    const encoder = encoding.createEncoder()
    encoding.writeVarUint(encoder, MSG_AWARENESS)
    encoding.writeVarUint8Array(encoder, update)
    this._sendBinary(encoding.toUint8Array(encoder))
  }

  private _sendBinary(data: Uint8Array) {
    if (this.ws && this.connected && this.ws.readyState === WebSocket.OPEN) {
      // Send as base64 JSON (Django consumer expects JSON)
      this.ws.send(JSON.stringify({
        type: 'yjs_sync',
        data: this._uint8ToB64(data),
      }))
    }
  }

  /** Send full Yjs state to server for persistence (via yjs_save message). */
  private _saveFullState() {
    if (!this.ws || !this.connected || this.ws.readyState !== WebSocket.OPEN) return
    try {
      const state = Y.encodeStateAsUpdate(this.ydoc)
      const message: { type: string; data: string; html?: string } = {
        type: 'yjs_save',
        data: this._uint8ToB64(state),
      }
      // Aktuelles HTML mitsenden — der Server hält damit content_encrypted
      // aktuell und legt gedrosselt Revisionen an.
      if (this.getHtml) {
        try {
          const html = this.getHtml()
          if (html) message.html = html
        } catch (e) {
          // HTML ist optional — Fehler hier dürfen das Yjs-Save nicht blockieren
        }
      }
      this.ws.send(JSON.stringify(message))
    } catch (e) {
      console.warn('Failed to save Yjs state:', e)
    }
  }

  private _uint8ToB64(arr: Uint8Array): string {
    let binary = ''
    for (let i = 0; i < arr.length; i++) {
      binary += String.fromCharCode(arr[i])
    }
    return btoa(binary)
  }

  private _b64ToUint8(b64: string): Uint8Array {
    const binary = atob(b64)
    const arr = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) {
      arr[i] = binary.charCodeAt(i)
    }
    return arr
  }

  private _scheduleReconnect() {
    if (this.destroyed) return
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    const base = Math.min(30000, 1000 * Math.pow(2, this.reconnectAttempts))
    const jitter = Math.floor(Math.random() * 500)
    const delay = base + jitter
    this.reconnectAttempts += 1
    this.reconnectTimer = setTimeout(() => this.connect(), delay)
  }

  destroy() {
    this.destroyed = true
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    if (this.saveTimer) clearInterval(this.saveTimer)
    if (this._beforeUnloadHandler) {
      window.removeEventListener('beforeunload', this._beforeUnloadHandler)
    }
    if (this._visibilityHandler) {
      document.removeEventListener('visibilitychange', this._visibilityHandler)
    }
    // Save final state before disconnecting
    this._saveFullState()
    this.ydoc.off('update', this._onDocUpdate)
    this.awareness.off('update', this._onAwarenessUpdate)
    awarenessProtocol.removeAwarenessStates(this.awareness, [this.ydoc.clientID], 'destroy')
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }
}


/**
 * Initialize real-time collaboration for a TipTap editor.
 *
 * Returns Yjs extensions that should be added to the TipTap editor config,
 * plus a destroy function for cleanup.
 */
export function initCollaboration(options: CollabOptions): CollabResult {
  const ydoc = new Y.Doc()
  const yXmlFragment = ydoc.getXmlFragment('default')
  const undoManager = new Y.UndoManager(yXmlFragment)
  const awareness = new awarenessProtocol.Awareness(ydoc)

  // IndexedDB persistence — load document from local cache instantly
  let idbProvider: IndexeddbPersistence | null = null
  if (options.documentId) {
    idbProvider = new IndexeddbPersistence(`mandari-doc-${options.documentId}`, ydoc)
  }

  // Set local awareness state (user info for cursor labels)
  awareness.setLocalStateField('user', {
    name: options.user.name,
    color: options.user.color,
    colorLight: options.user.color + '40',
  })

  // Track presence changes
  if (options.onPresenceChange) {
    const updatePresence = () => {
      const states = awareness.getStates()
      const users: CollabUser[] = []
      states.forEach((state, clientId) => {
        if (state.user) {
          users.push({
            name: state.user.name,
            color: state.user.color,
            clientId,
          })
        }
      })
      options.onPresenceChange!(users)
    }
    awareness.on('change', updatePresence)
  }

  // Create custom provider
  const provider = new DjangoYjsProvider(
    options.wsUrl,
    ydoc,
    awareness,
    options.onStatusChange,
    options.onInitialState,
    options.getHtml,
    options.onReloadRequired
  )

  // Build TipTap extensions
  const extensions = [
    Collaboration.configure({
      document: ydoc,
    }),
    CollaborationCursor.configure({
      provider: { awareness } as any,
      user: {
        name: options.user.name,
        color: options.user.color,
      },
    }),
  ]

  const destroy = () => {
    provider.destroy()
    if (idbProvider) {
      idbProvider.destroy()
    }
    ydoc.destroy()
  }

  return { ydoc, extensions, awareness, undoManager, destroy }
}
