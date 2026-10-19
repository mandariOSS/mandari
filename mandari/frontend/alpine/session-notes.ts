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
 *   Laden entspricht (`stand`). Hat jemand anders inzwischen gespeichert (409), stehen danach beide Fassungen
 *   untereinander im Editor – es geht nichts verloren.
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
const KONFLIKT_MARKE = '<p><strong>Ihre gleichzeitige Fassung:</strong></p>'

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
  let zuletztGespeichert = ''
  let wartenTimer = 0
  let leser: WebSocket | null = null
  let leserVersuche = 0
  let leserTimer = 0
  let beendet = false

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
        ? `${config.platzhalter}.${erwaehnen}`.replace('..', '.')
        : `${config.platzhalter}. Formatieren Sie mit der Leiste oder Strg+B, Strg+I, Strg+U.${erwaehnen}`
    },

    // ---- Live-Modus -----------------------------------------------------------------------------------------

    liveStarten(ausgang: string, zusammengefuehrt: boolean) {
      if (!config || !element || beendet) return
      this.art = 'live'
      gueltig = false
      ersterStand = true
      verbunden = false
      this.anwesende = []
      element.innerHTML = ''
      const personen = config.personen
      const erstellt = window.MandariEditor.createLiveNotesEditor({
        element,
        content: '',
        placeholder: this.platzhalter(),
        personen,
        wsUrl: wsAdresse(config.itemId),
        user: { name: 'Ich', color: '#6b7280' },
        onLocalUpdate: () => this.geaendert(),
        onRemoteUpdate: () => {
          if (!gueltig) this.freigeben()
        },
        onFormat: (format) => {
          this.format = format
        },
        onStatusChange: (s: CollabStatus) => this.verbindung(s),
        onPersisted: (stand: string) => {
          this.stand = stand
        },
        onReloadRequired: (grund) => this.zugriffGeaendert(grund),
        onJsonMessage: (msg) => this.nachricht(msg, ausgang, zusammengefuehrt),
      })
      editor = erstellt.editor
      collab = erstellt.collab
      // Schreiben erst mit gültigem Stand (vom Server, selbst gefüllt oder von den anderen), sonst gingen Eingaben
      // in einem leeren Zustand verloren
      editor.setEditable(false)
      this.bereit = true
      // Kommt keine Verbindung zustande (WebSocket gesperrt), arbeitet die Komponente wie bisher im Formular-Modus
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
      } else if (msg.type === 'praesenz') {
        this.praesenz(msg.art, msg.person as Anwesend)
      } else if (msg.type === 'neu_aufbauen') {
        this.neuAufbauen(String(msg.html ?? ''), String(msg.vorher ?? ''))
      }
    },

    /** Dieser Editor hat einen gültigen Stand: schreiben und speichern erlaubt. */
    freigeben() {
      gueltig = true
      window.clearTimeout(wartenTimer)
      if (this.status !== 'lesen') editor?.setEditable(true)
    },

    zustandErhalten(msg: any, ausgang: string, zusammengefuehrt: boolean) {
      if (!editor || !collab) return
      const erster = ersterStand
      ersterStand = false
      if (msg.data) {
        if (!erster) {
          // Nach einer Unterbrechung: Stand des Servers zusammenführen (Yjs führt doppelte Änderungen nicht doppelt aus)
          try {
            Y.applyUpdate(collab.ydoc, b64(msg.data))
          } catch {
            // Der Provider hat den Stand beim ersten Mal übernommen; ein defekter Stand ändert nichts
          }
        }
        this.freigeben()
        if (zusammengefuehrt) this.konflikt = true
        return
      }
      const html = String(msg.html ?? '')
      if (!erster) {
        // Der Zustand wurde verworfen (jemand hat ohne Verbindung gespeichert): neu aufbauen
        this.neuAufbauen(html, zuletztGespeichert)
        return
      }
      if (msg.seed) {
        // Dieser Editor füllt den Zustand aus den Notizen (genau einer je TOP)
        this.freigeben()
        const inhalt = zusammengefuehrt ? ausgang : html
        if (text(inhalt) || inhalt.includes('<li')) editor.commands.setContent(inhalt)
        if (zusammengefuehrt) this.konflikt = true
        return
      }
      if (!text(html) && !html.includes('<li')) {
        // Noch keine Notizen: nichts zu verlieren
        this.freigeben()
        return
      }
      // Inhalt kommt über die Synchronisierung von dem Editor, der ihn füllt; bleibt er aus, neu verbinden
      wartenTimer = window.setTimeout(() => {
        if (!gueltig && this.art === 'live') this.neuAufbauen(html, html)
      }, INHALT_WARTEN_MS)
    },

    verbindung(s: CollabStatus) {
      if (this.art !== 'live') return
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

    /** Neu aufbauen: Die Notizen wurden ohne Verbindung geändert, der gemeinsame Zustand gilt nicht mehr. */
    neuAufbauen(html: string, vorher: string) {
      if (!element || beendet) return
      const eigene = editor && gueltig ? editor.getHTML() : ''
      const basis = vorher || zuletztGespeichert
      const offen = Boolean(eigene) && text(eigene) !== text(basis) && text(eigene) !== text(html)
      const ausgang = offen ? `${html}${KONFLIKT_MARKE}${eigene}` : html
      window.clearTimeout(timer)
      window.clearTimeout(wartenTimer)
      const altEditor = editor
      const altCollab = collab
      editor = null
      collab = null
      altCollab?.destroy()
      altEditor?.destroy()
      ausstehend = offen
      this.liveStarten(ausgang, offen)
    },

    /** Ohne Verbindung: wie bisher über das Formular speichern (aktueller Inhalt bleibt im Editor). */
    zumFormular(ausgang: string) {
      if (!element) return
      const inhalt = editor && gueltig ? editor.getHTML() : ausgang
      window.clearTimeout(wartenTimer)
      const altEditor = editor
      const altCollab = collab
      editor = null
      collab = null
      altCollab?.destroy()
      altEditor?.destroy()
      this.anwesende = []
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
      if (this.art === 'live') await this.liveSpeichern(true)
      else await this.speichern(false)
    },

    /** Beim Verlassen der Seite oder Austausch des TOPs: über den WebSocket, sonst über das Formular. */
    verlassen() {
      if (!ausstehend || !editor) return
      if (this.art === 'live' && verbunden && this.status !== 'getrennt' && gueltig) void this.liveSpeichern(false)
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
          zuletztGespeichert = html
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

    async speichern(keepalive: boolean): Promise<void> {
      if (!editor || !config) return
      ausstehend = false
      const html = editor.getHTML()
      if (this.art !== 'live') this.status = 'speichert'
      const lauf = (async () => {
        try {
          let res = await aktion(
            { action: 'notizen', item_id: config?.itemId ?? '', html, stand: this.stand },
            keepalive,
          )
          let daten = (await res.json()) as Antwort
          if (res.status === 409 && editor && this.art === 'live') {
            // Live ohne Verbindung: beide Fassungen sofort zusammen speichern (der Editor wird danach neu aufgebaut)
            this.konflikt = true
            const beide = `${daten.html ?? ''}${KONFLIKT_MARKE}${html}`
            res = await aktion(
              { action: 'notizen', item_id: config?.itemId ?? '', html: beide, stand: daten.stand ?? '' },
              keepalive,
            )
            daten = (await res.json()) as Antwort
            if (res.ok) zuletztGespeichert = beide
          }
          if (res.ok) {
            this.stand = daten.stand ?? this.stand
            this.gespeichertUm = daten.gespeichert_um ?? ''
            if (this.art === 'live') {
              zuletztGespeichert = zuletztGespeichert || html
              this.status = verbunden && this.status !== 'getrennt' ? 'gespeichert' : 'getrennt'
            } else {
              this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
            }
          } else if (res.status === 409 && editor) {
            // Gleichzeitig gespeichert: beide Fassungen behalten (der aktuelle Editorinhalt, nicht nur der
            // gesendete Stand – sonst gingen Eingaben während der Anfrage verloren)
            this.konflikt = true
            this.stand = daten.stand ?? ''
            const eigene = editor.getHTML()
            editor.commands.setContent(`${daten.html ?? ''}${KONFLIKT_MARKE}${eigene}`, { emitUpdate: false })
            ausstehend = true
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
