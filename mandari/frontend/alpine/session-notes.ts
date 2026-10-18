/**
 * Notizen eines TOPs in der laufenden Fraktionssitzung (Alpine-Komponente `sitzungsNotizen`, Issue #874).
 *
 * Formatierbar wie im Dokumenteditor (TipTap, schmale Werkzeugleiste), ohne Speichern-Knopf: Änderungen gehen
 * nach kurzer Pause, vor jedem TOP-Wechsel und beim Verlassen der Seite an den Server. Der Server speichert nur,
 * wenn sein Stand dem Stand beim Laden entspricht (`stand`). Hat jemand anders inzwischen gespeichert (409),
 * stehen danach beide Fassungen untereinander im Editor – es geht nichts verloren.
 *
 * Wer nur mitliest, bekommt keinen Editor (der Server rendert die Notizen als HTML).
 * Konfiguration per `{{ notizen_config|json_script:"fs-notizen-config" }}`; Markup: `_protokoll.html`.
 */

import type { Editor } from '@tiptap/core'
import { defineComponent } from '../js/alpine/component'
import { readJsonScript } from '../js/json-script'
import { aktion, notizenAbmelden, notizenAnmelden } from './faction-session'

interface NotizenConfig {
  itemId: string
  stand: string
  editierbar: boolean
  platzhalter: string
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

type Status = 'gespeichert' | 'ungespeichert' | 'speichert' | 'fehler'

/** Pause nach der letzten Eingabe, bis gespeichert wird */
const PAUSE_MS = 1200
/** Neuer Versuch nach einem Fehler */
const WIEDERHOLEN_MS = 5000
const KONFLIKT_MARKE = '<p><strong>Ihre gleichzeitige Fassung:</strong></p>'

export const sitzungsNotizen = defineComponent(() => {
  let editor: Editor | null = null
  let config: NotizenConfig | null = null
  let timer = 0
  let laufend: Promise<void> | null = null
  let ausstehend = false
  let beimVerlassen: (() => void) | null = null

  return {
    bereit: false,
    status: 'gespeichert' as Status,
    gespeichertUm: '',
    konflikt: false,
    stand: '',
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
      config = readJsonScript<NotizenConfig>('fs-notizen-config')
      this.stand = config?.stand ?? ''
      const MandariEditor = window.MandariEditor
      const element = this.$refs.notiz
      if (!config?.editierbar || !MandariEditor || !element) return
      const inhalt = element.innerHTML
      element.innerHTML = ''
      editor = MandariEditor.createNotesEditor({
        element,
        content: inhalt,
        placeholder: `${config.platzhalter}. Formatieren Sie mit der Leiste oder Strg+B, Strg+I, Strg+U.`,
        onUpdate: () => this.geaendert(),
        onFormat: (format) => {
          this.format = format
        },
      })
      this.bereit = true
      notizenAnmelden(this)
      beimVerlassen = () => {
        if (ausstehend) void this.speichern(true)
      }
      window.addEventListener('pagehide', beimVerlassen)
    },

    destroy() {
      window.clearTimeout(timer)
      if (beimVerlassen) window.removeEventListener('pagehide', beimVerlassen)
      // Rest sichern, falls der TOP ohne vorheriges Speichern ausgetauscht wurde
      if (ausstehend) void this.speichern(true)
      notizenAbmelden(this)
      const alt = editor
      editor = null
      // Erst nach dem Lesen des Inhalts (speichern) zerstören
      window.setTimeout(() => alt?.destroy(), 0)
    },

    geaendert() {
      ausstehend = true
      this.status = 'ungespeichert'
      window.clearTimeout(timer)
      timer = window.setTimeout(() => void this.sichern(), PAUSE_MS)
    },

    /** Ausstehende Änderungen speichern und warten, bis der Server geantwortet hat. */
    async sichern(): Promise<void> {
      window.clearTimeout(timer)
      if (laufend) await laufend
      if (ausstehend) await this.speichern(false)
    },

    async speichern(keepalive: boolean): Promise<void> {
      if (!editor || !config) return
      ausstehend = false
      const html = editor.getHTML()
      this.status = 'speichert'
      const lauf = (async () => {
        try {
          const res = await aktion(
            { action: 'notizen', item_id: config?.itemId ?? '', html, stand: this.stand },
            keepalive,
          )
          const daten = (await res.json()) as Antwort
          if (res.ok) {
            this.stand = daten.stand ?? this.stand
            this.gespeichertUm = daten.gespeichert_um ?? ''
            this.status = ausstehend ? 'ungespeichert' : 'gespeichert'
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
      if (ausstehend && !keepalive) {
        window.clearTimeout(timer)
        const fehlgeschlagen = (this.status as Status) === 'fehler'
        timer = window.setTimeout(() => void this.sichern(), fehlgeschlagen ? WIEDERHOLEN_MS : 0)
      }
    },

    statusText(): string {
      if (this.status === 'speichert') return 'Wird gespeichert …'
      if (this.status === 'ungespeichert') return 'Änderungen werden gleich gespeichert'
      if (this.status === 'fehler') return 'Nicht gespeichert, neuer Versuch läuft'
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
