/**
 * Tagesordnung der Planung einer Fraktionssitzung (Alpine-Komponente `fsTagesordnung`, Issue #1071, Muster
 * sitzung.html „Planung“).
 *
 * - **Ziehen:** TOPs und Unterpunkte am Griff verschieben, je Teil (öffentlich, nichtöffentlich). SortableJS wie
 *   das Aufgabenboard (Vendor-Skript, global `window.Sortable`). Nach dem Ablegen geht die neue Stelle an die
 *   bisherige Aktion `move_item` (`direction=to`, `position` ab 0 unter den Geschwistern, `parent_id`); der Server
 *   antwortet mit der neuen Tagesordnung. Auch in den anderen Teil (`teil`, Issue #1160): Aus dem nichtöffentlichen in
 *   den öffentlichen Teil erst nach einer Rückfrage, denn dann sehen ihn alle Mitglieder. Scheitert das Speichern, lädt
 *   die Tagesordnung den Stand des Servers.
 * - **Ebene (Issue #1285):** Die Liste bleibt flach, Unterpunkte stehen direkt unter ihrem TOP. Wer beim Ziehen
 *   `EINRUECKEN_PX` nach rechts geht, macht den TOP zum Unterpunkt des TOPs darüber (aus 4 wird 3.1), nach links
 *   wird ein Unterpunkt zum eigenen TOP. Die Ablage zeigt Einrückung und künftige Nummer. Grenzen wie beim Server:
 *   eine Ebene, ein TOP mit Unterpunkten wird kein Unterpunkt (seine Unterpunkte ziehen ausgeblendet mit), nichts
 *   unter den Genehmigungs-TOP; liegt die Ablage zwischen einem TOP und seinen Unterpunkten, wird sie zum Unterpunkt.
 * - **Tastatur:** Griff fokussieren, Pfeil hoch oder runter (`move_item` up/down), Pfeil rechts rückt ein (`in`),
 *   Pfeil links rückt aus (`out`); der Fokus bleibt am Griff, eine Ansage nennt die neue Nummer. Die Einträge im Menü
 *   je TOP („Nach oben“, „Nach unten“, „Zum Unterpunkt von …“, „Zum eigenen TOP machen“) bleiben.
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
/** So weit nach rechts (links) gezogen wird ein TOP zum Unterpunkt (ein Unterpunkt zum eigenen TOP) */
const EINRUECKEN_PX = 32

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
    onStart: (event: ZiehEreignis) => void
    onChange: () => void
    onEnd: (event: ZiehEreignis) => void
  },
) => unknown

function sortable(): SortableKlasse | null {
  return (window as unknown as { Sortable?: SortableKlasse }).Sortable ?? null
}

/** Laufendes Ziehen: Zeile (Sortable bewegt sie als Ablage durch die Liste), Zeiger und Ebene */
interface Ziehen {
  zeile: HTMLElement
  startX: number
  x: number
  startEbene: number
  ebene: number
  /** Nummer der Zeile vor dem Ziehen, zum Zurücksetzen */
  nr: string
  /** Unterpunkte eines gezogenen TOPs: ausgeblendet, sie gehen mit */
  mitgezogen: HTMLElement[]
}

function istZeile(el: Element | null): el is HTMLElement {
  return el instanceof HTMLElement && el.classList.contains('fs-top') && !el.classList.contains('fs-mitgezogen')
}

/** Nächste sichtbare Zeile vor oder nach `zeile` (ohne ausgeblendete Unterpunkte des gezogenen TOPs) */
function nachbar(zeile: HTMLElement, richtung: 'previousElementSibling' | 'nextElementSibling'): HTMLElement | null {
  let el = zeile[richtung]
  while (el && !istZeile(el)) el = el[richtung]
  return el instanceof HTMLElement ? el : null
}

/** TOP, unter den eine Ablage direkt hinter `vorher` rücken würde: der TOP selbst oder der TOP seines Unterpunkts */
function elternFuer(vorher: HTMLElement | null): HTMLElement | null {
  if (!vorher) return null
  const eltern = vorher.dataset.eltern
  const top = eltern ? document.getElementById(`top-${eltern}`) : vorher
  // Der Genehmigungs-TOP ist nicht verschiebbar und nimmt keine Unterpunkte auf
  return top?.hasAttribute('data-verschiebbar') ? top : null
}

/** Zeilen vor `zeile` (bis ausschließlich `ab`), die `passt` erfüllen, ohne die Zeile selbst und Ausgeblendetes */
function davorZaehlen(zeile: HTMLElement, passt: (el: HTMLElement) => boolean, ab?: HTMLElement): number {
  let zahl = 0
  let el = nachbar(zeile, 'previousElementSibling')
  while (el && el !== ab) {
    if (passt(el)) zahl++
    el = nachbar(el, 'previousElementSibling')
  }
  return zahl
}

/** Künftige Nummer einer Zeile der obersten Ebene: Zeilen der obersten Ebene davor (mit Genehmigungs-TOP) + 1 */
function nummerOben(zeile: HTMLElement, gezogen: HTMLElement): string {
  const n = davorZaehlen(zeile, (el) => !el.dataset.eltern && el !== gezogen) + 1
  return zeile.parentElement?.dataset.teil === 'internal' ? `NÖ ${n}` : String(n)
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
    _ziehen: null as Ziehen | null,
    /** Zeigerposition beim Greifen und während des Ziehens */
    _zeigerX: 0,

    init() {
      standMelden = (s) => this.uebernehmeStand(s)
      // Zeiger verfolgen: beim Greifen (Startpunkt) und während des Ziehens (Ebene)
      const zeiger = (x: number) => {
        this._zeigerX = x
        if (this._ziehen) {
          this._ziehen.x = x
          this.ebeneZeigen()
        }
      }
      this.$el.addEventListener('pointerdown', (e: PointerEvent) => zeiger(e.clientX), true)
      document.addEventListener('pointermove', (e: PointerEvent) => {
        if (this._ziehen) zeiger(e.clientX)
      })
      document.addEventListener(
        'touchmove',
        (e: TouchEvent) => {
          if (this._ziehen && e.touches[0]) zeiger(e.touches[0].clientX)
        },
        { passive: true },
      )
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
          onStart: (event) => this.ziehenBeginnt(event.item),
          onChange: () => this.ebeneZeigen(),
          onEnd: (event) => {
            this.$el.classList.remove('fs-zieht')
            void this.abgelegt(event)
          },
        })
      }
    },

    ziehenBeginnt(zeile: HTMLElement): void {
      this.$el.classList.add('fs-zieht')
      const id = zeile.dataset.id || ''
      const mitgezogen = zeile.dataset.eltern
        ? []
        : Array.from(zeile.parentElement?.querySelectorAll<HTMLElement>(`:scope > [data-eltern="${id}"]`) ?? [])
      for (const kind of mitgezogen) kind.classList.add('fs-mitgezogen')
      const ebene = zeile.dataset.eltern ? 1 : 0
      this._ziehen = {
        zeile,
        startX: this._zeigerX,
        x: this._zeigerX,
        startEbene: ebene,
        ebene,
        nr: zeile.querySelector('.nr')?.textContent ?? '',
        mitgezogen,
      }
      this.ebeneZeigen()
    },

    /**
     * Ebene der Ablage aus Zeiger und Nachbarn: gewünscht ist die Ebene beim Greifen, um eine verschoben, wenn der
     * Zeiger `EINRUECKEN_PX` nach rechts oder links gewandert ist; möglich ist ein Unterpunkt nur unter einem
     * verschiebbaren TOP darüber und nur ohne eigene Unterpunkte, nötig, wenn darunter ein Unterpunkt folgt.
     */
    ebeneZeigen(): void {
      const z = this._ziehen
      if (!z) return
      const dx = z.x - z.startX
      const gewuenscht = z.startEbene + (dx >= EINRUECKEN_PX ? 1 : dx <= -EINRUECKEN_PX ? -1 : 0)
      const eltern = elternFuer(nachbar(z.zeile, 'previousElementSibling'))
      const danach = nachbar(z.zeile, 'nextElementSibling')
      const hoechstens = eltern && z.mitgezogen.length === 0 ? 1 : 0
      const mindestens = danach?.dataset.eltern ? 1 : 0
      z.ebene = Math.min(Math.max(gewuenscht, mindestens), hoechstens)
      z.zeile.dataset.ebeneNeu = String(z.ebene)
      const nummer = z.zeile.querySelector('.nr')
      if (!nummer) return
      if (z.ebene === 1 && eltern) {
        const stelle = davorZaehlen(z.zeile, (el) => el.dataset.eltern === eltern.dataset.id, eltern) + 1
        nummer.textContent = `${nummerOben(eltern, z.zeile)}.${stelle}`
      } else {
        nummer.textContent = nummerOben(z.zeile, z.zeile)
      }
    },

    /** Ende des Ziehens: Ablage zurück auf den Stand vor dem Ziehen, Unterpunkte wieder einblenden */
    ziehenEndet(): Ziehen | null {
      const z = this._ziehen
      this._ziehen = null
      if (!z) return null
      for (const kind of z.mitgezogen) kind.classList.remove('fs-mitgezogen')
      delete z.zeile.dataset.ebeneNeu
      const nummer = z.zeile.querySelector('.nr')
      if (nummer) nummer.textContent = z.nr
      return z
    },

    /** Nach dem Ablegen: neue Stelle und Ebene speichern, beim Wechsel des Teils mit dem Ziel-Teil */
    async abgelegt(event: ZiehEreignis): Promise<void> {
      const zeile = event.item
      // Stelle und Eltern noch mit ausgeblendeten Unterpunkten bestimmen, dann aufräumen
      const z = this._ziehen
      const ebene = z?.ebene ?? 0
      const startEbene = z?.startEbene ?? 0
      const eltern = ebene === 1 ? elternFuer(nachbar(zeile, 'previousElementSibling')) : null
      // Stelle unter den Geschwistern: Unterpunkte desselben TOPs bzw. verschiebbare TOPs der obersten Ebene
      const position = eltern
        ? davorZaehlen(zeile, (el) => el.dataset.eltern === eltern.dataset.id, eltern)
        : davorZaehlen(zeile, (el) => !el.dataset.eltern && el.hasAttribute('data-verschiebbar'))
      this.ziehenEndet()
      const andererTeil = event.from !== event.to
      if (!andererTeil && event.oldIndex === event.newIndex && ebene === startEbene) return
      const id = zeile.dataset.id || ''
      if (!id) return
      const werte: Record<string, string> = {
        direction: 'to',
        position: String(position),
        parent_id: eltern?.dataset.id || '',
      }
      const teil = event.to.dataset.teil || ''
      // Anderer Teil, oder ein nichtöffentlicher Unterpunkt eines öffentlichen TOPs wird eigener TOP im öffentlichen Teil
      const wirdOeffentlich = teil === 'public' && !eltern && zeile.hasAttribute('data-intern')
      if (andererTeil || wirdOeffentlich) {
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
      if (werte.teil) {
        this.ansage =
          werte.teil === 'internal'
            ? 'TOP in den nichtöffentlichen Teil verschoben.'
            : 'TOP in den öffentlichen Teil verschoben.'
      } else if (eltern && startEbene === 0) {
        this.ansage = 'TOP ist jetzt ein Unterpunkt.'
      } else if (!eltern && startEbene === 1) {
        this.ansage = 'Unterpunkt ist jetzt ein eigener TOP.'
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

    /** Pfeil hoch oder runter am Griff, Pfeil rechts rückt ein, Pfeil links rückt aus */
    taste(event: KeyboardEvent, richtung: 'up' | 'down' | 'in' | 'out'): void {
      const griff = event.currentTarget as HTMLElement | null
      const id = griff?.dataset.griff || ''
      if (!id) return
      const istUnterpunkt = Boolean(griff?.closest<HTMLElement>('.fs-top')?.dataset.eltern)
      if ((richtung === 'in' && istUnterpunkt) || (richtung === 'out' && !istUnterpunkt)) return
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
