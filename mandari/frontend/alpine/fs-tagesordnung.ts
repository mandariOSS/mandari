/**
 * Tagesordnung der Planung einer Fraktionssitzung (Alpine-Komponente `fsTagesordnung`, Issue #1071, Muster
 * sitzung.html „Planung“).
 *
 * - **Ziehen:** TOPs der obersten Ebene am Griff verschieben, je Teil (öffentlich, nichtöffentlich). SortableJS wie
 *   das Aufgabenboard (Vendor-Skript, global `window.Sortable`). Nach dem Ablegen geht die neue Stelle an die
 *   bisherige Aktion `move_item` (`direction=to`, `position` ab 0); der Server antwortet mit der neuen Tagesordnung.
 *   Auch in den anderen Teil (`teil`, Issue #1160): Aus dem nichtöffentlichen in den öffentlichen Teil erst nach einer
 *   Rückfrage, denn dann sehen ihn alle Mitglieder. Scheitert das Speichern, lädt die Tagesordnung den Stand des Servers.
 *   Tastatur: Griff fokussieren, Pfeil hoch oder runter (`move_item` up/down); der Fokus bleibt am Griff, eine
 *   Ansage nennt die neue Nummer. Die Einträge „Nach oben“/„Nach unten“ im Menü je TOP bleiben.
 * - **Sofort speichern:** Titel und Beschreibung des aufgeklappten TOPs speichern beim Verlassen des Felds bzw. mit
 *   Enter im Titel (JSON an `work:faction_item_fields`). Speicherdienst der Vorbereitung (frontend/js/speichern.ts):
 *   Wiederholung bei Störung, Anhalten bei abgelaufener Anmeldung oder fehlendem zweitem Faktor
 *   (`two_factor_setup_required`, Link zum Einrichten). Bis zur Bestätigung liegt die Eingabe in der Sicherung im
 *   Browser (frontend/js/eingaben-sicherung.ts); beim nächsten Aufruf wird sie erneut gesendet, wenn der Server noch
 *   den Stand hat, auf dem sie beruht, sonst zur Übernahme angeboten.
 *
 * Die Tagesordnung (`#agenda-container`) tauscht htmx nach jeder Aktion aus. Die Komponente sitzt darüber und richtet
 * Ziehen nach jedem Austausch neu ein. Markup: templates/work/faction/neu/_vb_tagesordnung.html, _vb_top.html.
 * Konfiguration: `{{ to_config|json_script:"fs-to-config" }}`.
 */

import { defineComponent } from '../js/alpine/component'
import { beiAbmeldungSperren, Eingabensicherung, type GesicherterBereich, vergleichen } from '../js/eingaben-sicherung'
import { readJsonScript } from '../js/json-script'
import { Speicherdienst, type SpeicherStand } from '../js/speichern'

interface ToConfig {
  /** Aktions-URL der Sitzung (move_item) */
  actionUrl: string
  /** Adresse zum Speichern der Felder eines TOPs; PLATZHALTER steht für den TOP */
  felderUrl: string
  accountId: string
  orgId: string
  meetingId: string
}

/** Platzhalter-UUID in `felderUrl` (wie PANEL_ITEM_PLACEHOLDER) */
const PLATZHALTER = '00000000-0000-0000-0000-000000000000'
const CONFIG_ID = 'fs-to-config'
const BEREICH = 'top'
const FELDER = ['title', 'description'] as const
type Feld = (typeof FELDER)[number]
const GESPEICHERT_MS = 2500

// ---- SortableJS (Vendor-Skript ohne Typpaket; Typ hier nur lokal, task-board.ts hat einen eigenen) --------------

interface ZiehEreignis {
  item: HTMLElement
  from: HTMLElement
  to: HTMLElement
  oldIndex?: number
  newIndex?: number
}

type SortableKlasse = new (
  el: HTMLElement,
  optionen: {
    group: string
    handle: string
    draggable: string
    emptyInsertThreshold: number
    animation: number
    ghostClass: string
    forceFallback: boolean
    fallbackOnBody: boolean
    onStart: () => void
    onEnd: (event: ZiehEreignis) => void
  },
) => unknown

function sortable(): SortableKlasse | null {
  return (window as unknown as { Sortable?: SortableKlasse }).Sortable ?? null
}

/** Ein gesicherter, noch nicht gespeicherter Stand zur Übernahme (Konflikt oder Stand des Servers unbekannt) */
interface Angebot {
  top: string
  felder: Partial<Record<Feld, string>>
  /** „TOP 3, Titel und Beschreibung“ */
  text: string
}

function felderText(felder: string[]): string {
  const namen = felder.map((f) => (f === 'title' ? 'Titel' : 'Beschreibung'))
  return namen.join(' und ')
}

export const fsTagesordnung = defineComponent(() => {
  const config = readJsonScript<ToConfig>(CONFIG_ID) ?? {
    actionUrl: '',
    felderUrl: '',
    accountId: '',
    orgId: '',
    meetingId: '',
  }
  const sicherung = new Eingabensicherung(config.accountId, config.orgId, config.meetingId)
  beiAbmeldungSperren(sicherung)
  let standMelden: (stand: SpeicherStand) => void = () => {}
  const speicher = new Speicherdienst({ beiAenderung: (stand) => standMelden(stand), sicherung })
  /** Endgültig gescheiterte Eingaben je TOP, für „Erneut versuchen“ */
  const gescheitert = new Map<string, Partial<Record<Feld, string>>>()
  let gespeichertTimer = 0

  return {
    /** '', 'speichert', 'gespeichert', 'wiederholt', 'anmeldung', 'fehler' */
    stand: '',
    meldung: '',
    anmeldungZiel: '',
    /** Ansage für Vorleser (Verschieben) */
    ansage: '',
    angebote: [] as Angebot[],
    /** Nach dem Austausch fokussieren (Verschieben per Tastatur) */
    _fokusNach: '',
    _ansageNach: false,

    init() {
      standMelden = (s) => this.uebernehmeStand(s)
      this.einrichten()
      this.sicherungPruefen()
      this.$el.addEventListener('htmx:afterSettle', (event: Event) => {
        const ziel = (event as CustomEvent<{ target?: Element }>).detail?.target
        if (ziel instanceof HTMLElement && ziel.id === 'agenda-container') this.nachAustausch()
      })
      const verbergen = () => {
        speicher.verbergen = true
        sicherung.festschreiben()
      }
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') verbergen()
        else {
          speicher.verbergen = false
          speicher.jetztWiederholen()
        }
      })
      window.addEventListener('pagehide', verbergen)
      window.addEventListener('beforeunload', (e: BeforeUnloadEvent) => {
        verbergen()
        window.setTimeout(() => {
          speicher.verbergen = document.visibilityState === 'hidden'
        }, 0)
        if (speicher.mussWarnen()) {
          e.preventDefault()
          e.returnValue = ''
        }
      })
      window.addEventListener('online', () => speicher.jetztWiederholen())
      window.addEventListener('focus', () => speicher.jetztWiederholen())
    },

    // ---------- Ziehen ----------

    /** Nach jedem Austausch der Tagesordnung: Ziehen einrichten, Stand des Servers merken, Fokus zurück */
    nachAustausch(): void {
      this.einrichten()
      if (this._fokusNach) {
        const griff = this.$el.querySelector<HTMLElement>(`[data-griff="${this._fokusNach}"]`)
        if (griff) {
          griff.focus()
          if (this._ansageNach) {
            const nr = griff.closest('.fs-top')?.querySelector('.nr')?.textContent?.trim()
            this.ansage = nr ? `TOP verschoben, jetzt ${nr}.` : 'TOP verschoben.'
          }
        }
        this._fokusNach = ''
        this._ansageNach = false
      }
    },

    einrichten(): void {
      this.serverstandMerken()
      const Sortable = sortable()
      if (!Sortable) return
      for (const liste of Array.from(this.$el.querySelectorAll<HTMLElement>('ol.fs-tops[data-ziehbar]'))) {
        new Sortable(liste, {
          // Beide Teile gehören zusammen; den nichtöffentlichen zeigt der Server nur Vereidigten
          group: 'fs-tagesordnung',
          handle: '.fs-griff',
          draggable: '[data-verschiebbar]',
          emptyInsertThreshold: 24,
          animation: 150,
          ghostClass: 'bewegt',
          // Ziehen über Maus-/Zeigerereignisse statt HTML5 Drag and Drop: gleiches Verhalten in allen Browsern
          forceFallback: true,
          fallbackOnBody: true,
          onStart: () => this.$el.classList.add('fs-zieht'),
          onEnd: (event) => {
            this.$el.classList.remove('fs-zieht')
            void this.abgelegt(event)
          },
        })
      }
    },

    /** Nach dem Ablegen: neue Stelle speichern, beim Wechsel des Teils mit dem Ziel-Teil */
    async abgelegt(event: ZiehEreignis): Promise<void> {
      const andererTeil = event.from !== event.to
      if (!andererTeil && event.oldIndex === event.newIndex) return
      const zeilen = Array.from(event.to.querySelectorAll<HTMLElement>(':scope > [data-verschiebbar]'))
      const position = zeilen.indexOf(event.item)
      const id = event.item.dataset.id || ''
      if (!id || position < 0) return
      const werte: Record<string, string> = { direction: 'to', position: String(position) }
      if (andererTeil) {
        const teil = event.to.dataset.teil || ''
        if (teil === 'public') {
          const bestaetigt = await window.confirmAction({
            title: 'In den öffentlichen Teil verschieben?',
            message:
              'Dann sehen alle Mitglieder diesen TOP, auch Nicht-Vereidigte. Er steht auch in der nächsten Aktualisierung.',
            confirmText: 'Verschieben',
            variant: 'danger',
          })
          if (!bestaetigt) {
            this.standNeuLaden()
            return
          }
        }
        werte.teil = teil
      }
      this.beiFehlerNeuLaden()
      this.verschieben(id, werte, false)
      if (andererTeil) {
        this.ansage =
          werte.teil === 'internal'
            ? 'TOP in den nichtöffentlichen Teil verschoben.'
            : 'TOP in den öffentlichen Teil verschoben.'
      }
    },

    /** Die Zeile liegt schon am neuen Platz: Scheitert das Speichern, zeigt die Seite wieder den Stand des Servers */
    beiFehlerNeuLaden(): void {
      document.body.addEventListener(
        'htmx:afterRequest',
        (event: Event) => {
          if (!(event as CustomEvent<{ successful?: boolean }>).detail?.successful) this.standNeuLaden()
        },
        { once: true },
      )
    },

    standNeuLaden(): void {
      window.htmx.trigger(document.body, 'agenda-refresh')
    },

    /** Pfeil hoch oder runter am Griff */
    taste(event: KeyboardEvent, richtung: 'up' | 'down'): void {
      const griff = event.currentTarget as HTMLElement | null
      const id = griff?.dataset.griff || ''
      if (!id) return
      event.preventDefault()
      this.verschieben(id, { direction: richtung }, true)
    },

    verschieben(id: string, werte: Record<string, string>, tastatur: boolean): void {
      const offen = document.getElementById('fs-offen') as HTMLInputElement | null
      this._fokusNach = tastatur ? id : ''
      this._ansageNach = tastatur
      void window.htmx.ajax('post', config.actionUrl, {
        target: '#agenda-container',
        swap: 'innerHTML',
        headers: { 'X-Work-Gestaltung': 'neu' },
        values: { action: 'move_item', item_id: id, offen: offen?.value || '', ...werte },
      })
      if (!tastatur) this.ansage = 'TOP verschoben.'
    },

    // ---------- Sofort speichern ----------

    felderUrl(top: string): string {
      return config.felderUrl.replace(PLATZHALTER, top)
    },

    /** Stand des Servers der Felder auf der Seite (Grundlage für den Vergleich gesicherter Eingaben) */
    serverstandMerken(): void {
      for (const feld of Array.from(this.$el.querySelectorAll<HTMLInputElement>('[data-top-feld]'))) {
        const top = feld.dataset.top || ''
        if (top)
          sicherung.serverstandSetzen({ top, bereich: BEREICH }, { [feld.dataset.topFeld || '']: feld.defaultValue })
      }
    },

    /** Feld verlassen (change) oder Enter im Titel */
    feldSpeichern(feld: HTMLInputElement | HTMLTextAreaElement): void {
      const top = feld.dataset.top || ''
      const name = feld.dataset.topFeld as Feld | undefined
      if (!top || !name || feld.value === feld.defaultValue) return
      if (name === 'title' && !feld.value.trim()) {
        this.stand = 'fehler'
        this.meldung = 'Der Titel darf nicht leer sein. Der bisherige Titel bleibt.'
        feld.value = feld.defaultValue
        return
      }
      this.senden(top, { [name]: feld.value })
    },

    senden(top: string, felder: Partial<Record<Feld, string>>): void {
      this.stand = 'speichert'
      void speicher
        .senden({
          url: this.felderUrl(top),
          body: felder,
          wiederholbar: true,
          sicherung: { top, bereich: BEREICH },
          bezeichnung: (body) => felderText(Object.keys(body)),
        })
        .then((ergebnis) => {
          if (ergebnis.ok) {
            gescheitert.delete(top)
            this.gespeichert(top, felder, String(ergebnis.daten.title || ''))
          } else if (!ergebnis.ersetzt) {
            gescheitert.set(top, { ...(gescheitert.get(top) || {}), ...felder })
          }
        })
    },

    /** Gespeichert: Felder und Titel in der Zeile auf den neuen Stand, Haken kurz zeigen */
    gespeichert(top: string, felder: Partial<Record<Feld, string>>, titel: string): void {
      for (const [name, wert] of Object.entries(felder)) {
        const feld = this.$el.querySelector<HTMLInputElement>(`[data-top="${top}"][data-top-feld="${name}"]`)
        if (feld) {
          // Bei Wiederholung nach einem Austausch zeigt das Feld noch den alten Stand des Servers
          if (feld.value === feld.defaultValue && document.activeElement !== feld) feld.value = wert
          feld.defaultValue = wert
        }
      }
      if (titel) {
        const link = document.querySelector(`#top-${CSS.escape(top)} .fs-top-titel`)
        if (link) link.textContent = titel
      }
      if (!speicher.stand().sendet && !speicher.stand().wiederholt && gescheitert.size === 0) {
        this.stand = 'gespeichert'
        this.meldung = ''
        window.clearTimeout(gespeichertTimer)
        gespeichertTimer = window.setTimeout(() => {
          if (this.stand === 'gespeichert') this.stand = ''
        }, GESPEICHERT_MS)
      }
    },

    uebernehmeStand(s: SpeicherStand): void {
      this.anmeldungZiel = s.anmeldungZiel
      if (s.anmeldung === 'zweiter_faktor') {
        this.stand = 'anmeldung'
        this.meldung = 'Nicht gespeichert: Richten Sie zuerst den zweiten Faktor ein. Work sendet danach erneut.'
      } else if (s.anmeldung === 'abgelaufen') {
        this.stand = 'anmeldung'
        this.meldung =
          'Nicht gespeichert: Ihre Anmeldung ist abgelaufen. Melden Sie sich neu an; Work sendet danach erneut.'
      } else if (s.wiederholt > 0) {
        this.stand = 'wiederholt'
        this.meldung = s.offline
          ? 'Keine Verbindung. Ihre Eingabe ist im Browser gesichert, Work versucht es erneut.'
          : 'Noch nicht gespeichert. Work versucht es erneut.'
      } else if (s.fehler) {
        this.stand = 'fehler'
        this.meldung = s.fehler
      } else if (s.sendet > 0) {
        this.stand = 'speichert'
      }
    },

    /** „Erneut versuchen“: Wartendes sofort senden, endgültig Gescheitertes noch einmal */
    erneut(): void {
      speicher.jetztWiederholen()
      const offen = [...gescheitert.entries()]
      gescheitert.clear()
      for (const [top, felder] of offen) this.senden(top, felder)
      if (!offen.length && this.stand === 'fehler') this.stand = ''
    },

    // ---------- Gesicherte Eingaben aus einem früheren Aufruf ----------

    /** Wert eines Felds beim Server laut Seite; `null`, wenn die Seite ihn nicht kennt (TOP zugeklappt) */
    serverWert(top: string, feld: string): unknown {
      const zeile = document.getElementById(`top-${top}`)
      if (!zeile) return undefined
      if (!(FELDER as readonly string[]).includes(feld)) return undefined
      const eingabe = this.$el.querySelector<HTMLInputElement>(`[data-top="${top}"][data-top-feld="${feld}"]`)
      if (eingabe) return eingabe.defaultValue
      if (feld === 'title') return zeile.dataset.titel ?? null
      return null
    },

    sicherungPruefen(): void {
      const angebote: Angebot[] = []
      for (const { top, eintrag } of sicherung.alle()) {
        if (!document.getElementById(`top-${top}`)) {
          // TOP gibt es nicht mehr oder er ist nicht (mehr) bearbeitbar
          sicherung.entfernen(top)
          continue
        }
        const bereich: GesicherterBereich | undefined = eintrag.bereiche[BEREICH]
        if (!bereich) continue
        const { erledigt, angebot } = vergleichen(bereich, (feld) => this.serverWert(top, feld))
        sicherung.vergessen({ top, bereich: BEREICH }, erledigt)
        const sofort: Partial<Record<Feld, string>> = {}
        const anbieten: Partial<Record<Feld, string>> = {}
        for (const [feld, a] of Object.entries(angebot)) {
          if (!(FELDER as readonly string[]).includes(feld) || typeof a.wert !== 'string') continue
          // Der Server hat noch den Stand, auf dem die Eingabe beruht: einfach erneut senden
          if (!a.konflikt && a.server !== null) sofort[feld as Feld] = a.wert
          else anbieten[feld as Feld] = a.wert
        }
        if (Object.keys(sofort).length) this.senden(top, sofort)
        if (Object.keys(anbieten).length) {
          const nr = document.querySelector(`#top-${CSS.escape(top)} .nr`)?.textContent?.trim() || ''
          angebote.push({ top, felder: anbieten, text: `TOP ${nr}, ${felderText(Object.keys(anbieten))}` })
        }
      }
      this.angebote = angebote
    },

    angebotUebernehmen(): void {
      const liste = this.angebote
      this.angebote = []
      for (const a of liste) this.senden(a.top, a.felder)
    },

    angebotVerwerfen(): void {
      for (const a of this.angebote) sicherung.vergessen({ top: a.top, bereich: BEREICH }, a.felder)
      this.angebote = []
    },
  }
})
