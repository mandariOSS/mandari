/**
 * Notizen eines TOPs in der Fraktionssitzung (Alpine-Komponente `sitzungsNotizen`, Issue #874).
 *
 * Formatierbar wie im Dokumenteditor (TipTap, schmale Werkzeugleiste), ohne Speichern-Knopf; mit @ erwähnen Sie
 * Mitglieder (sie bekommen eine Benachrichtigung). Drei Arten:
 *
 * - **Live** (laufende Sitzung, `live` in der Konfiguration): Mehrere schreiben gleichzeitig (Yjs über den WebSocket
 *   des TOPs, apps/work/faction/live.py), sehen die Cursor der anderen und wer gerade im Protokoll ist. Gespeichert
 *   wird über den WebSocket nach kurzer Pause, vor jedem TOP-Wechsel und beim Verlassen der Seite. Ohne Verbindung
 *   (WebSocket gesperrt, Netz weg) speichert die Komponente wie im Formular-Modus; kommt keine Verbindung zustande,
 *   arbeitet sie ganz im Formular-Modus.
 * - **Formular**: Änderungen gehen nach kurzer Pause an den Server. Er speichert nur, wenn sein Stand dem Stand beim
 *   Laden entspricht (`stand`). Hat jemand anders inzwischen gespeichert (409), führt `zusammenfuehren` beide Fassungen
 *   zusammen: Ist die eine nur ein älterer Stand der anderen, gilt die neuere; nur echte Abweichungen stehen
 *   untereinander (Issue #1166). Es geht nichts verloren.
 * - **Lesen** (ohne Protokollrecht): kein Editor; im Live-Fall kommt der gespeicherte Stand über den WebSocket.
 *
 * Konfiguration per `{{ notizen_config|json_script:"fs-notizen-config" }}`; Markup: `_protokoll.html`. Stehen mehrere
 * Editoren auf einer Seite (Protokoll nach der Sitzung, `neu/_pr_dokument.html`), nennt `data-konfig` am Element die
 * Kennung des eigenen json_script.
 */

import type { Editor } from '@tiptap/core'
import * as Y from 'yjs'
import type { CollabResult, CollabStatus } from '../editor/collaboration'
import type { ErwaehnbarePerson } from '../editor/extensions/erwaehnung'
import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'
import { aktion, notizenAbmelden, notizenAnmelden, vorAnfragenSichern } from './faction-session'
import { zusammenfuehren } from './notizen-zusammenfuehren'

interface NotizenConfig {
  itemId: string
  stand: string
  editierbar: boolean
  platzhalter: string
  /** Ohne Werkzeugleiste (Protokoll nach der Sitzung): Platzhalter ohne Hinweis auf die Leiste */
  leiste?: boolean
  /** Live-Mitschreiben über den WebSocket des TOPs */
  live?: boolean
  /** Vorschlagsliste für @-Erwähnungen */
  personen?: ErwaehnbarePerson[]
  /** Eigene Mitgliedschaft (Auslöser einer Erwähnung) */
  ich?: string
}

interface Antwort {
  stand?: string
  gespeichert_um?: string
  html?: string
  konflikt?: boolean
}

export interface NotizenFormat {
  bold: boolean
  italic: boolean
  underline: boolean
  heading: boolean
  bulletList: boolean
  orderedList: boolean
  link: boolean
}

/** Wer gerade im Protokoll dieses TOPs ist (eine Verbindung je Person) */
export interface Anwesend {
  id: string
  user: string
  name: string
  kuerzel: string
  color: string
  schreibt: boolean
}

type Status = 'gespeichert' | 'ungespeichert' | 'speichert' | 'fehler' | 'getrennt' | 'lesen'
type Art = 'aus' | 'formular' | 'live' | 'lesen'

/** Pause nach der letzten Eingabe, bis gespeichert wird */
const PAUSE_MS = 1200
/** Neuer Versuch nach einem Fehler */
const WIEDERHOLEN_MS = 5000
/** So lange wartet der Live-Modus auf die Verbindung, dann Formular-Modus */
const VERBINDEN_MS = 4000
/** So lange wartet ein Editor ohne eigenen Stand auf den Inhalt der anderen, dann baut er neu auf */
const INHALT_WARTEN_MS = 5000

function wsAdresse(itemId: string): string {
  const protokoll = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protokoll}//${window.location.host}/ws/fraktion/notizen/${itemId}/`
}

/** Reiner Text eines HTML-Stücks (Vergleich, ob noch ungespeicherte Eingaben im Editor stehen) */
function text(html: string): string {
  const doc = new DOMParser().parseFromString(html || '', 'text/html')
  return (doc.body.textContent || '').replace(/\s+/g, ' ').trim()
}

function b64(data: string): Uint8Array {
  const binaer = atob(data)
  const bytes = new Uint8Array(binaer.length)
  for (let i = 0; i < binaer.length; i++) bytes[i] = binaer.charCodeAt(i)
  return bytes
}

export const sitzungsNotizen = defineComponent(() => {
  let editor: Editor | null = null
  let collab: CollabResult | null = null
  let config: NotizenConfig | null = null
  let element: HTMLElement | null = null
  let timer = 0
  let laufend: Promise<void> | null = null
  let ausstehend = false
  let beimVerlassen: (() => void) | null = null
  // Live: darf dieser Editor speichern (hat er einen gültigen Stand)?
  let gueltig = false
  let ersterStand = true
  let verbunden = false
  // Stand der Notizen, auf dem dieser Editor beruht (geladen, zuletzt gespeichert oder neu aufgebaut): Basis für das
  // Zusammenführen (Issue #1166)
  let basisFassung = ''
  // Nach einem Neuaufbau: eigene, zusammengeführte Fassung, die noch in den gemeinsamen Zustand muss
  let nachholen: string | null = null
  let konfliktNachAufbau = false
  let wartenTimer = 0
  let leser: WebSocket | null = null
  let leserVersuche = 0
  let leserTimer = 0
  let beendet = false
  // Notizen, mit denen der Live-Editor gestartet ist (Rückfall auf das Formular)
  let ausgangAktuell = ''

  return {
    art: 'aus' as Art,
    bereit: false,
    status: 'gespeichert' as Status,
    gespeichertUm: '',
    konflikt: false,
    stand: '',
    ich: '',
    anwesende: [] as Anwesend[],
    format: {
      bold: false,
      italic: false,
      underline: false,
      heading: false,
      bulletList: false,
      orderedList: false,
      link: false,
    } as NotizenFormat,

    init() {
      // Mehrere Editoren auf einer Seite (Protokoll nach der Sitzung, Issue #1071): eigene Konfiguration je Element
      config = readJsonScript<NotizenConfig>(this.$el.dataset.konfig || 'fs-notizen-config')
      this.stand = config?.stand ?? ''
      element = this.$refs.notiz ?? null
      if (!config || !element) return
      const live = Boolean(config.live) && 'WebSocket' in window
      if (config.editierbar && window.MandariEditor) {
        const inhalt = element.innerHTML
        basisFassung = inhalt
        if (live) this.liveStarten(inhalt, false)
        else this.formularStarten(inhalt)
        notizenAnmelden(this)
        vorAnfragenSichern()
        beimVerlassen = () => this.verlassen()
        window.addEventListener('pagehide', beimVerlassen)
      } else if (live && !config.editierbar) {
        this.lesenStarten()
      }
    },

    destroy() {
      beendet = true
      window.clearTimeout(timer)
      window.clearTimeout(wartenTimer)
      window.clearTimeout(leserTimer)
      if (beimVerlassen) window.removeEventListener('pagehide', beimVerlassen)
      // Rest sichern, falls der TOP ohne vorheriges Speichern ausgetauscht wurde
      if (ausstehend) this.verlassen()
      notizenAbmelden(this)
      leser?.close()
      leser = null
      const altEditor = editor
      const altCollab = collab
      editor = null
      collab = null
      // Erst nach dem Lesen des Inhalts (speichern) zerstören
      window.setTimeout(() => {
        altCollab?.destroy()
        altEditor?.destroy()
      }, 0)
    },

    // ---- Formular-Modus -------------------------------------------------------------------------------------

    formularStarten(inhalt: string) {
      if (!config || !element) return
      this.art = 'formular'
      element.innerHTML = ''
      editor = window.MandariEditor.createNotesEditor({
        element,
        content: inhalt,
        placeholder: this.platzhalter(),
        personen: config.personen,
        ich: config.ich,
        onUpdate: () => this.geaendert(),
        onFormat: (format) => {
          this.format = format
        },
      })
      this.bereit = true
    },

    platzhalter(): string {
      if (!config) return ''
      const erwaehnen = config.personen?.length ? ' Mit @ erwähnen Sie Personen.' : ''
      return config.leiste === false
        ? `${config.platzhalter}${erwaehnen ? `.${erwaehnen}` : ''}`
        : `${config.platzhalter}. Formatieren Sie mit der Leiste oder Strg+B, Strg+I, Strg+U.${erwaehnen}`
    },

    // ---- Live-Modus -----------------------------------------------------------------------------------------

    liveStarten(ausgang: string, zusammengefuehrt: boolean, konflikt = false) {
      if (!config || !element || beendet) return
      this.art = 'live'
      gueltig = false
      ersterStand = true
      verbunden = false
      nachholen = zusammengefuehrt ? ausgang : null
      konfliktNachAufbau = konflikt
      this.anwesende = []
      element.innerHTML = ''
      const personen = config.personen
      // Meldungen nur vom aktuellen Editor annehmen: Ein schon abgebauter (neuAufbauen, zumFormular) meldet sonst noch
      // sein Schließen, und der neue wiche auf das Formular aus (Issue #1166)
      let dieser: CollabResult | null = null
      const aktuell = () => dieser !== null && collab === dieser
      const erstellt = window.MandariEditor.createLiveNotesEditor({
        element,
        content: '',
        placeholder: this.platzhalter(),
        personen,
        ich: config.ich,
        wsUrl: wsAdresse(config.itemId),
        user: { name: 'Ich', color: '#6b7280' },
        onLocalUpdate: () => {
          if (aktuell()) this.geaendert()
        },
        onRemoteUpdate: () => {
          if (aktuell() && !gueltig) this.freigeben()
        },
        onFormat: (format) => {
          this.format = format
        },
        onStatusChange: (s: CollabStatus) => {
          if (aktuell()) this.verbindung(s)
        },
        onPersisted: (stand: string) => {
          if (aktuell()) this.stand = stand
        },
        onReloadRequired: (grund) => {
          if (aktuell()) this.zugriffGeaendert(grund)
        },
        onJsonMessage: (msg) => {
          if (aktuell()) this.nachricht(msg, ausgang, zusammengefuehrt)
        },
      })
      editor = erstellt.editor
      collab = erstellt.collab
      dieser = erstellt.collab
      // Speichern erst mit gültigem Stand (vom Server, selbst gefüllt oder von den anderen); was bis dahin getippt
      // wird, bleibt im Editor und wird mit dem Stand zusammengeführt
      this.bereit = true
      // Kommt keine Verbindung zustande (WebSocket gesperrt), arbeitet die Komponente wie bisher im Formular-Modus
      ausgangAktuell = ausgang
      wartenTimer = window.setTimeout(() => {
        if (!verbunden && this.art === 'live') this.zumFormular(ausgang)
      }, VERBINDEN_MS)
    },

    nachricht(msg: any, ausgang: string, zusammengefuehrt: boolean) {
      if (!msg || typeof msg.type !== 'string' || !editor) return
      if (msg.type === 'connected') {
        verbunden = true
        this.ich = String(msg.user?.user ?? '')
        const person = msg.user as Anwesend | undefined
        if (person && collab) {
          // Eigener Name und eigene Farbe am Cursor, wie sie die anderen sehen
          collab.awareness.setLocalStateField('user', {
            name: person.name,
            color: person.color,
            colorLight: `${person.color}40`,
          })
        }
        if (msg.user?.access_level !== 'edit') this.zugriffGeaendert('access_changed')
        window.clearTimeout(wartenTimer)
      } else if (msg.type === 'yjs_state') {
        this.zustandErhalten(msg, ausgang, zusammengefuehrt)
      } else if (msg.type === 'yjs_saved') {
        this.gespeichertUm = String(msg.gespeichert_um ?? '')
      } else if (msg.type === 'fremd_gespeichert') {
        // Jemand anders hat den gemeinsamen Stand gespeichert. Seine Änderungen kamen über dieselbe Verbindung vorher an
        // und stehen schon im Editor: Das ist jetzt die Basis für ein späteres Zusammenführen (Issue #1166)
        if (gueltig && typeof msg.html === 'string') basisFassung = msg.html
        if (msg.gespeichert_um) this.gespeichertUm = String(msg.gespeichert_um)
      } else if (msg.type === 'praesenz') {
        this.praesenz(msg.art, msg.person as Anwesend)
      } else if (msg.type === 'neu_aufbauen') {
        this.neuAufbauen(String(msg.html ?? ''), typeof msg.vorher === 'string' ? msg.vorher : null)
      }
    },

    /** Dieser Editor hat einen gültigen Stand: schreiben und speichern erlaubt. */
    freigeben() {
      gueltig = true
      window.clearTimeout(wartenTimer)
      if (konfliktNachAufbau) this.konflikt = true
      konfliktNachAufbau = false
      // Nach einem Neuaufbau kam der Stand von einem anderen Editor: eigene Fassung jetzt einarbeiten (Issue #1166)
      const eigene = nachholen
      nachholen = null
      if (eigene !== null && editor) {
        const gemeinsam = editor.getHTML()
        const ergebnis = zusammenfuehren(eigene, gemeinsam, basisFassung)
        if (ergebnis.konflikt) this.konflikt = true
        if (ergebnis.html !== gemeinsam) {
          editor.commands.setContent(ergebnis.html)
          ausstehend = true
        }
      }
      // Vorher Getipptes jetzt speichern
      if (ausstehend) this.geaendert()
    },

    /** Was vor dem ersten Stand getippt wurde (sonst leer) */
    getippt(): string {
      return editor && !editor.isEmpty ? editor.getHTML() : ''
    },

    /** Vor der Freigabe: eigene Fassung aus einem Neuaufbau und seitdem Getipptes (nichts davon ist gespeichert) */
    offeneFassung(): string {
      return `${nachholen ?? ''}${this.getippt()}`
    },

    zustandErhalten(msg: any, ausgang: string, zusammengefuehrt: boolean) {
      if (!editor || !collab) return
      const erster = ersterStand
      ersterStand = false
      if (msg.data) {
        // Stand des Servers gleich selbst anwenden, auch beim ersten Mal: Der Provider tut es erst nach dieser
        // Nachricht, und freigeben() führte die eigene Fassung sonst mit einem leeren Dokument zusammen (doppelter
        // Inhalt, Issue #1166). Yjs führt doppelte Änderungen nicht doppelt aus.
        try {
          Y.applyUpdate(collab.ydoc, b64(msg.data))
        } catch {
          // Ein defekter Stand ändert nichts; der Provider meldet ihn selbst
        }
        this.freigeben()
        return
      }
      const html = String(msg.html ?? '')
      if (!erster) {
        // Der Zustand wurde verworfen (jemand hat ohne Verbindung gespeichert): neu aufbauen
        this.neuAufbauen(html, null)
        return
      }
      if (!zusammengefuehrt) basisFassung = html
      if (msg.seed) {
        // Dieser Editor füllt den Zustand aus den Notizen (genau einer je TOP); schon Getipptes kommt darunter
        const getippt = this.getippt()
        const inhalt = `${zusammengefuehrt ? ausgang : html}${getippt}`
        nachholen = null
        if (text(inhalt) || inhalt.includes('<li')) editor.commands.setContent(inhalt)
        this.freigeben()
        return
      }
      if (!text(html) && !html.includes('<li')) {
        if (nachholen === null) {
          // Noch keine Notizen und nichts Eigenes: nichts zu verlieren
          this.freigeben()
          return
        }
        // Eigene Fassung nach einem Neuaufbau: Einfügen darf nur der Editor mit der Zusage, sonst fügte jeder Editor
        // dieselbe Fassung ein und Yjs verdoppelte sie (Issue #1166). Sein Inhalt kommt über die Synchronisierung
        // (dann wird zusammengeführt); bleibt er aus, selbst einfügen
        wartenTimer = window.setTimeout(() => {
          if (!gueltig && this.art === 'live') this.freigeben()
        }, INHALT_WARTEN_MS)
        return
      }
      // Inhalt kommt über die Synchronisierung von dem Editor, der ihn füllt; bleibt er aus, neu verbinden
      wartenTimer = window.setTimeout(() => {
        if (!gueltig && this.art === 'live') this.neuAufbauen(html, html)
      }, INHALT_WARTEN_MS)
    },

    verbindung(s: CollabStatus) {
      if (this.art !== 'live') return
      if (s === 'disconnected' && !verbunden) {
        // Keine Verbindung möglich (etwa ohne WebSocket-Weiterleitung): sofort wie bisher über das Formular
        this.zumFormular(ausgangAktuell)
        return
      }
      if (s === 'connected') {
        if (this.status === 'getrennt') this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
        if (ausstehend) this.geaendert()
      } else if (s === 'disconnected' && verbunden) {
        this.status = 'getrennt'
        this.anwesende = []
      }
    },

    praesenz(art: string, person: Anwesend | undefined) {
      if (!person?.id) return
      const ohne = this.anwesende.filter((p) => p.id !== person.id)
      this.anwesende = art === 'da' ? [...ohne, person] : ohne
    },

    /** Andere Personen im Protokoll, je Person einmal (mehrere Fenster zählen einfach) */
    andere(): Anwesend[] {
      const gesehen = new Map<string, Anwesend>()
      for (const p of this.anwesende) {
        if (p.user === this.ich) continue
        const bisher = gesehen.get(p.user)
        if (!bisher || (p.schreibt && !bisher.schreibt)) gesehen.set(p.user, p)
      }
      return [...gesehen.values()]
    },

    andereText(): string {
      const liste = this.andere()
      if (!liste.length) return ''
      return `Gerade im Protokoll: ${liste.map((p) => `${p.name}${p.schreibt ? ', schreibt mit' : ', liest mit'}`).join('; ')}`
    },

    /**
     * Neu aufbauen: Die Notizen wurden ohne Verbindung geändert, der gemeinsame Zustand gilt nicht mehr.
     *
     * `vorher` = Notizen vor dieser Speicherung (`null`, wenn der Server sie nicht kennt). Eigene Eingaben, die dort
     * noch nicht stehen, werden mit den neuen Notizen zusammengeführt; ein älterer Stand derselben Eingabe ergibt
     * keinen Konflikt mehr (Issue #1166).
     */
    neuAufbauen(html: string, vorher: string | null) {
      if (!element || beendet) return
      const eigene = editor && gueltig ? editor.getHTML() : this.offeneFassung()
      const ergebnis = eigene ? zusammenfuehren(eigene, html, vorher ?? basisFassung) : { html, konflikt: false }
      const offen = ergebnis.html !== html
      basisFassung = html
      window.clearTimeout(timer)
      window.clearTimeout(wartenTimer)
      const altEditor = editor
      const altCollab = collab
      editor = null
      collab = null
      altCollab?.destroy()
      altEditor?.destroy()
      ausstehend = offen
      this.liveStarten(ergebnis.html, offen, ergebnis.konflikt)
    },

    /** Ohne Verbindung: wie bisher über das Formular speichern (aktueller Inhalt bleibt im Editor). */
    zumFormular(ausgang: string) {
      if (!element) return
      const inhalt = editor && gueltig ? editor.getHTML() : `${ausgang}${this.getippt()}`
      window.clearTimeout(wartenTimer)
      const altEditor = editor
      const altCollab = collab
      editor = null
      collab = null
      altCollab?.destroy()
      altEditor?.destroy()
      this.anwesende = []
      nachholen = null
      if (konfliktNachAufbau) this.konflikt = true
      konfliktNachAufbau = false
      this.formularStarten(inhalt)
      if (ausstehend) this.geaendert()
    },

    zugriffGeaendert(grund?: string) {
      // Nach der Genehmigung oder ohne Protokollrecht: nur noch lesen; ohne Zugriff: Verbindung beenden
      window.clearTimeout(timer)
      ausstehend = false
      editor?.setEditable(false)
      this.status = 'lesen'
      if (grund === 'access_revoked') {
        const alt = collab
        collab = null
        alt?.destroy()
        this.anwesende = []
      }
    },

    // ---- Lesen (ohne Editor) --------------------------------------------------------------------------------

    lesenStarten() {
      if (!config || beendet) return
      this.art = 'lesen'
      const ws = new WebSocket(wsAdresse(config.itemId))
      leser = ws
      ws.onmessage = (event) => {
        let msg: any
        try {
          msg = JSON.parse(String(event.data))
        } catch {
          return
        }
        if (msg.type === 'connected') {
          leserVersuche = 0
          this.ich = String(msg.user?.user ?? '')
        } else if ((msg.type === 'yjs_state' || msg.type === 'inhalt') && element && msg.html) {
          if (element.innerHTML !== msg.html) element.innerHTML = msg.html
        } else if (msg.type === 'praesenz') {
          this.praesenz(msg.art, msg.person as Anwesend)
        }
      }
      ws.onclose = (event) => {
        if (leser !== ws) return
        leser = null
        this.anwesende = []
        // Ohne Zugriff (4401/4403) nicht erneut versuchen
        if (beendet || event.code === 4401 || event.code === 4403) return
        leserVersuche += 1
        leserTimer = window.setTimeout(() => this.lesenStarten(), Math.min(30000, 1000 * 2 ** leserVersuche))
      }
    },

    // ---- Speichern ------------------------------------------------------------------------------------------

    geaendert() {
      if (this.status === 'lesen') return
      ausstehend = true
      if (this.status !== 'getrennt') this.status = 'ungespeichert'
      window.clearTimeout(timer)
      timer = window.setTimeout(() => void this.sichern(), PAUSE_MS)
    },

    /** Ausstehende Änderungen speichern und warten, bis der Server geantwortet hat. */
    async sichern(): Promise<void> {
      window.clearTimeout(timer)
      if (laufend) await laufend
      if (!ausstehend) return
      // Vor der Freigabe kann der Live-Editor nicht speichern: eine offene eigene Fassung über das Formular sichern
      if (this.art === 'live' && !gueltig && nachholen !== null) await this.speichern(false, this.offeneFassung())
      else if (this.art === 'live') await this.liveSpeichern(true)
      else await this.speichern(false)
    },

    /** Beim Verlassen der Seite oder Austausch des TOPs: über den WebSocket, sonst über das Formular. */
    verlassen() {
      if (!ausstehend || !editor) return
      if (this.art === 'live' && !gueltig && nachholen !== null) void this.speichern(true, this.offeneFassung())
      else if (this.art === 'live' && verbunden && this.status !== 'getrennt' && gueltig) void this.liveSpeichern(false)
      else if (this.art !== 'live' || gueltig) void this.speichern(true)
    },

    async liveSpeichern(mitRueckfall: boolean): Promise<void> {
      if (!collab || !editor || !gueltig || this.status === 'lesen') return
      ausstehend = false
      const html = editor.getHTML()
      this.status = 'speichert'
      const verbindung = collab
      const lauf = (async () => {
        const ok = await verbindung.flush(4000)
        if (ok) {
          basisFassung = html
          this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
          return
        }
        ausstehend = true
        if (mitRueckfall && this.status !== 'lesen') {
          // Keine Bestätigung über den WebSocket (Verbindung weg): über das Formular sichern
          await this.speichern(false)
        } else if (this.status !== 'lesen') {
          this.status = 'getrennt'
        }
      })()
      laufend = lauf
      await lauf
      if (laufend === lauf) laufend = null
    },

    /** Über das Formular speichern; `inhalt` statt des Editorinhalts (offene Fassung vor der Freigabe). */
    async speichern(keepalive: boolean, inhalt?: string): Promise<void> {
      if (!editor || !config) return
      ausstehend = false
      const html = inhalt ?? editor.getHTML()
      if (this.art !== 'live') this.status = 'speichert'
      const lauf = (async () => {
        try {
          let res = await aktion(
            { action: 'notizen', item_id: config?.itemId ?? '', html, stand: this.stand },
            keepalive,
          )
          let daten = (await res.json()) as Antwort
          let gesendet = html
          if (res.status === 409 && editor && this.art === 'live') {
            // Live ohne Verbindung: beide Fassungen zusammenführen und sofort speichern (der Editor wird danach neu
            // aufgebaut). Steht auf dem Server schon alles (gleicher oder neuerer Stand), bleibt es dabei.
            const server = daten.html ?? ''
            const ergebnis = zusammenfuehren(html, server, basisFassung)
            if (ergebnis.konflikt) this.konflikt = true
            if (ergebnis.html === server) {
              this.stand = daten.stand ?? this.stand
              this.gespeichertUm = daten.gespeichert_um ?? this.gespeichertUm
              basisFassung = server
              this.status = verbunden && this.status !== 'getrennt' ? 'gespeichert' : 'getrennt'
              return
            }
            gesendet = ergebnis.html
            res = await aktion(
              { action: 'notizen', item_id: config?.itemId ?? '', html: gesendet, stand: daten.stand ?? '' },
              keepalive,
            )
            daten = (await res.json()) as Antwort
          }
          if (res.ok) {
            this.stand = daten.stand ?? this.stand
            this.gespeichertUm = daten.gespeichert_um ?? ''
            basisFassung = gesendet
            if (this.art === 'live') {
              this.status = verbunden && this.status !== 'getrennt' ? 'gespeichert' : 'getrennt'
            } else {
              this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
            }
          } else if (res.status === 409 && editor) {
            // Gleichzeitig gespeichert: mit dem aktuellen Editorinhalt zusammenführen (nicht nur mit dem gesendeten
            // Stand, sonst gingen Eingaben während der Anfrage verloren). Nur echte Abweichungen stehen untereinander.
            const server = daten.html ?? ''
            const eigene = editor.getHTML()
            const ergebnis = zusammenfuehren(eigene, server, basisFassung)
            this.stand = daten.stand ?? ''
            basisFassung = server
            if (ergebnis.konflikt) this.konflikt = true
            if (ergebnis.html !== eigene) editor.commands.setContent(ergebnis.html, { emitUpdate: false })
            if (ergebnis.html === server) {
              this.gespeichertUm = daten.gespeichert_um ?? this.gespeichertUm
              this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
            } else {
              ausstehend = true
            }
          } else {
            this.status = 'fehler'
            ausstehend = true
          }
        } catch {
          this.status = 'fehler'
          ausstehend = true
        }
      })()
      laufend = lauf
      await lauf
      if (laufend === lauf) laufend = null
      if (ausstehend && !keepalive && this.art === 'formular') {
        window.clearTimeout(timer)
        const fehlgeschlagen = (this.status as Status) === 'fehler'
        timer = window.setTimeout(() => void this.sichern(), fehlgeschlagen ? WIEDERHOLEN_MS : 0)
      }
    },

    statusText(): string {
      if (this.status === 'speichert') return 'Wird gespeichert …'
      if (this.status === 'ungespeichert') return 'Änderungen werden gleich gespeichert'
      if (this.status === 'fehler') return 'Nicht gespeichert, neuer Versuch läuft'
      if (this.status === 'getrennt') return 'Verbindung unterbrochen, Ihre Eingaben bleiben erhalten'
      if (this.status === 'lesen') return 'Nur lesen'
      return this.gespeichertUm ? `Automatisch gespeichert um ${this.gespeichertUm}` : 'Automatisch gespeichert'
    },

    befehl(art: string) {
      if (!editor) return
      const kette = editor.chain().focus()
      if (art === 'bold') kette.toggleBold().run()
      else if (art === 'italic') kette.toggleItalic().run()
      else if (art === 'underline') kette.toggleUnderline().run()
      else if (art === 'heading') kette.toggleHeading({ level: 3 }).run()
      else if (art === 'bulletList') kette.toggleBulletList().run()
      else if (art === 'orderedList') kette.toggleOrderedList().run()
      else if (art === 'link') this.link()
      else if (art === 'erwaehnen') this.erwaehnen()
    },

    /** „@“ an der Schreibmarke einfügen; die Vorschlagsliste öffnet sich von selbst. */
    erwaehnen() {
      if (!editor) return
      const { from } = editor.state.selection
      const davor = from > 1 ? editor.state.doc.textBetween(from - 1, from, '\n', ' ') : ''
      editor
        .chain()
        .focus()
        .insertContent(davor && !/\s/.test(davor) ? ' @' : '@')
        .run()
    },

    link() {
      if (!editor) return
      if (editor.isActive('link')) {
        editor.chain().focus().extendMarkRange('link').unsetLink().run()
        return
      }
      const adresse = window.prompt('Adresse des Links (https://…)')?.trim()
      if (!adresse) return
      if (!/^(https?:\/\/|mailto:)/i.test(adresse)) {
        window.alert('Bitte eine Adresse mit https:// angeben.')
        return
      }
      editor.chain().focus().extendMarkRange('link').setLink({ href: adresse }).run()
    },

    zurAufgabe() {
      document.getElementById('fs-auf-titel')?.focus()
    },
  }
})
