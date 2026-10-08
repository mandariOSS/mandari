/**
 * Sitzungsansicht der laufenden Fraktionssitzung (Issue #874).
 *
 * Komponenten (registriert im Editor-Bundle `frontend/editor/index.ts`):
 * - `fraktionssitzung`: Seite – aktueller TOP, Anwesenheit (ab 2200 px als Spalte), Tagesordnung am Handy, Tasten J/K,
 *   Fehlermeldungen
 * - `sitzungsMenue`: Menü „Weitere Aktionen“ im Kopf
 * - `topKopf`: Kopf des TOPs, „Mehr“ nur, wenn eine Zeile gekürzt ist
 * - `sitzungsUnterlagen`: Reiter der Unterlagen (Überlauf in „n weitere“), Schriftgröße, Pfeiltasten
 * - `unterlageAnhaengen`: Unterlage in der Sitzung anhängen (Datei, RIS-Vorlage, eigenes Dokument)
 * - `beschlussLeiste`: Stimmenzähler der Leiste unten
 * - `anwesenheitsListe`: Anwesenheit vor Ort/online, Sitzungsleitung und Schriftführung
 * - `zustaendigAuswahl`: Personen und Personengruppen für Aufgaben (Suche und Auswahl kombiniert)
 *
 * Markup: `templates/work/faction/sitzung/`. Server-Daten per `json_script` (`fs-sitzung-config`,
 * `fs-zustaendig-config`) bzw. `data-*` am Element.
 */

import { defineComponent } from '../js/alpine/component'
import { showToast } from '../js/alpine/toast'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

interface SitzungUrls {
  aktion: string
  top: string
  seite: string
  platzhalter: string
}

function urls(): SitzungUrls {
  return readJsonScript<SitzungUrls>('fs-sitzung-config') ?? { aktion: '', top: '', seite: '', platzhalter: '' }
}

// ---- Notizen vor TOP-Wechsel sichern -----------------------------------------------------------

export interface NotizenSicherung {
  sichern(): Promise<void>
}

let aktiveNotizen: NotizenSicherung | null = null

export function notizenAnmelden(notizen: NotizenSicherung): void {
  aktiveNotizen = notizen
}

export function notizenAbmelden(notizen: NotizenSicherung): void {
  if (aktiveNotizen === notizen) aktiveNotizen = null
}

/** Ausstehende Notizen speichern (vor einem TOP-Wechsel oder einem Neuaufbau des TOPs). */
export async function notizenSichern(): Promise<void> {
  try {
    await aktiveNotizen?.sichern()
  } catch {
    // Fehler zeigt die Notizen-Komponente selbst an; der Wechsel geht trotzdem weiter
  }
}

/** POST an den Aktions-Endpunkt mit Formularwerten; liefert die Antwort (JSON oder Text). */
export async function aktion(werte: Record<string, string>, keepalive = false): Promise<Response> {
  const body = new FormData()
  for (const [key, value] of Object.entries(werte)) body.append(key, value)
  return fetch(urls().aktion, {
    method: 'POST',
    body,
    credentials: 'same-origin',
    keepalive,
    headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
  })
}

function tippt(target: EventTarget | null): boolean {
  const el = target instanceof HTMLElement ? target : null
  if (!el) return false
  return el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)
}

// ---- Seite ----------------------------------------------------------------------------------------

interface HtmxDetail {
  elt?: Element
  target?: Element
  xhr?: XMLHttpRequest
  issueRequest?: (skip: boolean) => void
  question?: string
}

/** Ab dieser Breite steht die Anwesenheit als eigene Spalte rechts (Prototyp Fassung 2), darunter klappt sie auf */
const BREIT = '(min-width: 2200px)'

export const fraktionssitzung = defineComponent(() => ({
  aktuell: '',
  anwesenheitOffen: false,
  tagesordnungOffen: false,
  breit: false,

  init() {
    this.aktuell = this.$el.dataset.aktuell ?? ''
    const breite = window.matchMedia(BREIT)
    this.breit = breite.matches
    breite.addEventListener('change', (event) => {
      this.breit = event.matches
    })
    const body = document.body
    // Vor einem TOP-Wechsel (und vor Aktionen, die den TOP neu aufbauen) die Notizen speichern
    body.addEventListener('htmx:confirm', (event) => {
      const detail = (event as CustomEvent<HtmxDetail>).detail
      const elt = detail.elt
      if (!elt?.closest('[data-top-wechsel], [data-sichern-vorher]') || detail.question) return
      event.preventDefault()
      void notizenSichern().then(() => detail.issueRequest?.(true))
    })
    // Neuer TOP: aktuellen TOP merken, Blatt am Handy schließen, Überschrift fokussieren
    body.addEventListener('htmx:afterSwap', (event) => {
      const target = (event as CustomEvent<HtmxDetail>).detail.target
      if (!(target instanceof HTMLElement) || target.id !== 'fs-fokus') return
      const neu = document.getElementById('fs-fokus')
      if (neu?.dataset.top && neu.dataset.top !== this.aktuell) {
        this.aktuell = neu.dataset.top
        this.tagesordnungOffen = false
        document.getElementById('fs-top-titel')?.focus({ preventScroll: true })
      }
    })
    // Feste Fehlermeldungen des Servers (4xx) als Hinweis zeigen; 5xx meldet htmx-setup.ts
    body.addEventListener('htmx:responseError', (event) => {
      const xhr = (event as CustomEvent<HtmxDetail>).detail.xhr
      if (!xhr || xhr.status >= 500) return
      const text = xhr.status === 403 ? 'Dafür fehlt Ihnen die Berechtigung.' : xhr.responseText
      showToast(text && text.length < 200 ? text : 'Die Aktion war nicht möglich.', 'error')
    })
  },

  istAktuell(id: string | undefined): boolean {
    return !!id && id === this.aktuell
  },

  anwesenheitUmschalten() {
    this.anwesenheitOffen = !this.anwesenheitOffen
  },

  /** Klick außerhalb der Anwesenheit schließt sie – außer auf den Knopf im Kopf, der sie umschaltet. */
  anwesenheitAussen(event: Event) {
    const ziel = event.target instanceof Element ? event.target : null
    if (ziel?.closest('[aria-controls="fs-anwesenheit"]')) return
    this.anwesenheitOffen = false
  },

  schliessen() {
    this.anwesenheitOffen = false
    this.tagesordnungOffen = false
  },

  /** J: nächster TOP, K: voriger TOP (nicht beim Schreiben) */
  taste(event: KeyboardEvent) {
    if (event.altKey || event.ctrlKey || event.metaKey || tippt(event.target)) return
    const richtung =
      event.key === 'j' || event.key === 'J' ? 'nach' : event.key === 'k' || event.key === 'K' ? 'vor' : ''
    if (!richtung) return
    const link = document.querySelector<HTMLElement>(`#fs-unten [data-nav="${richtung}"]`)
    if (link) {
      event.preventDefault()
      link.click()
    }
  },
}))

export const sitzungsMenue = defineComponent(() => ({
  offen: false,
  umschalten() {
    this.offen = !this.offen
  },
  schliessen() {
    this.offen = false
  },
}))

// ---- Kopf des TOPs --------------------------------------------------------------------------------

export const topKopf = defineComponent(() => {
  let beobachter: ResizeObserver | null = null
  let wurzel: HTMLElement | null = null
  return {
    offen: false,
    gekuerzt: false,
    init() {
      wurzel = this.$el
      const messen = () => {
        const zeilen = wurzel ? Array.from(wurzel.querySelectorAll<HTMLElement>('.fs-top-zeile2, .wf-verlauf li')) : []
        this.gekuerzt = zeilen.some((el) => el.scrollWidth > el.clientWidth + 1)
      }
      beobachter = new ResizeObserver(messen)
      beobachter.observe(this.$el)
      this.$nextTick(messen)
    },
    destroy() {
      beobachter?.disconnect()
    },
  }
})

// ---- Unterlagen -------------------------------------------------------------------------------------

const SCHRIFT_KEY = 'fs-dok-groesse'

interface WeitereUnterlage {
  schluessel: string
  titel: string
}

/** Von hinten Reiter verstecken, bis die Zeile nicht mehr überläuft; der gewählte Reiter bleibt sichtbar. */
function kuerzen(liste: HTMLElement, reiter: HTMLElement[], gewaehlt: string, versteckt: WeitereUnterlage[]): void {
  for (let i = reiter.length - 1; i >= 0 && liste.scrollWidth > liste.clientWidth + 1; i--) {
    const r = reiter[i]
    if (r.hidden || r.dataset.schluessel === gewaehlt) continue
    r.hidden = true
    versteckt.unshift({ schluessel: r.dataset.schluessel ?? '', titel: r.dataset.titel ?? '' })
  }
}

export const sitzungsUnterlagen = defineComponent(() => {
  let beobachter: ResizeObserver | null = null
  let reiterListe: HTMLElement | null = null
  return {
    gewaehlt: '',
    weitere: [] as WeitereUnterlage[],
    weitereOffen: false,
    groesse: 0,

    init() {
      this.gewaehlt = this.$el.dataset.aktiv ?? ''
      reiterListe = this.$refs.reiter ?? null
      const gespeichert = Number(window.localStorage.getItem(SCHRIFT_KEY) ?? '0')
      this.groesse = Number.isFinite(gespeichert) ? Math.max(-2, Math.min(4, gespeichert)) : 0
      this.anwenden()
      beobachter = new ResizeObserver(() => this.messen())
      if (reiterListe?.parentElement) beobachter.observe(reiterListe.parentElement)
      this.$nextTick(() => this.messen())
    },

    destroy() {
      beobachter?.disconnect()
    },

    istGewaehlt(schluessel: string | undefined): boolean {
      return !!schluessel && schluessel === this.gewaehlt
    },

    waehle(schluessel: string | undefined) {
      if (!schluessel) return
      this.gewaehlt = schluessel
      this.weitereOffen = false
      this.$nextTick(() => this.messen())
    },

    /** Reiter aus „n weitere“ wählen: denselben Knopf auslösen (lädt den Inhalt per HTMX) */
    zeige(schluessel: string) {
      const knopf = reiterListe?.querySelector<HTMLElement>(`[data-schluessel="${CSS.escape(schluessel)}"]`)
      knopf?.click()
    },

    /** Reiter, die nicht in die Zeile passen, ins Menü „n weitere“; der gewählte bleibt sichtbar. */
    messen() {
      const liste = reiterListe
      if (!liste) return
      const reiter = Array.from(liste.querySelectorAll<HTMLElement>('[role="tab"]'))
      for (const r of reiter) r.hidden = false
      const versteckt: WeitereUnterlage[] = []
      kuerzen(liste, reiter, this.gewaehlt, versteckt)
      this.weitere = versteckt
      // Der Knopf „n weitere“ erscheint erst nach Alpines Aktualisierung und nimmt der Zeile Platz: im nächsten
      // Frame noch einmal kürzen, sonst würde der letzte sichtbare Reiter abgeschnitten
      window.requestAnimationFrame(() => {
        const vorher = versteckt.length
        kuerzen(liste, reiter, this.gewaehlt, versteckt)
        if (versteckt.length !== vorher) this.weitere = [...versteckt]
      })
    },

    schrift(schritt: number) {
      this.groesse = Math.max(-2, Math.min(4, this.groesse + schritt))
      window.localStorage.setItem(SCHRIFT_KEY, String(this.groesse))
      this.anwenden()
    },

    anwenden() {
      const seite = reiterListe?.closest<HTMLElement>('.fs-sitzung')
      seite?.style.setProperty('--fs-dok-plus', `${this.groesse}px`)
    },

    /** Pfeiltasten, Pos1 und Ende wechseln zwischen den sichtbaren Reitern (ARIA-Reiter) */
    reiterTaste(event: KeyboardEvent) {
      if (!reiterListe || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
      const sichtbar = Array.from(reiterListe.querySelectorAll<HTMLElement>('[role="tab"]')).filter((r) => !r.hidden)
      const jetzt = sichtbar.findIndex((r) => r.dataset.schluessel === this.gewaehlt)
      let ziel = jetzt
      if (event.key === 'ArrowLeft') ziel = jetzt <= 0 ? sichtbar.length - 1 : jetzt - 1
      if (event.key === 'ArrowRight') ziel = jetzt >= sichtbar.length - 1 ? 0 : jetzt + 1
      if (event.key === 'Home') ziel = 0
      if (event.key === 'End') ziel = sichtbar.length - 1
      event.preventDefault()
      sichtbar[ziel]?.focus()
      sichtbar[ziel]?.click()
    },
  }
})

interface Treffer {
  id: string
  titel: string
  nummer?: string
  art?: string
  datum?: string
}

type AnhangArt = '' | 'datei' | 'vorlage' | 'dokument'

export const unterlageAnhaengen = defineComponent(() => {
  let item = ''
  let ziel = 'unterlagen'
  let anfrage = 0
  return {
    menue: false,
    dialog: '' as AnhangArt,
    suche: '',
    treffer: [] as Treffer[],
    hinweis: '',

    init() {
      item = this.$el.dataset.item ?? ''
      ziel = this.$el.dataset.ziel ?? 'unterlagen'
    },

    /** Für c-ui.alpine-modal: offen, solange eine Art gewählt ist; Escape und Klick daneben schließen */
    get dialogOffen(): boolean {
      return this.dialog !== ''
    },
    set dialogOffen(offen: boolean) {
      if (!offen) this.dialog = ''
    },

    titel(): string {
      if (this.dialog === 'datei') return 'Datei hochladen'
      if (this.dialog === 'vorlage') return 'RIS-Vorlage verknüpfen'
      return 'Eigenes Dokument verknüpfen'
    },

    oeffne(art: AnhangArt) {
      this.menue = false
      this.dialog = art
      this.suche = ''
      this.treffer = []
      this.hinweis = art === 'vorlage' ? 'Geben Sie mindestens zwei Zeichen ein.' : ''
      if (art === 'dokument') void this.suchen()
    },

    async suchen() {
      const art = this.dialog
      if (art !== 'vorlage' && art !== 'dokument') return
      const text = this.suche.trim()
      if (art === 'vorlage' && text.length < 2) {
        this.treffer = []
        this.hinweis = 'Geben Sie mindestens zwei Zeichen ein.'
        return
      }
      const nummer = ++anfrage
      const res = await aktion({ action: `${art}_suchen`, item_id: item, q: text })
      if (nummer !== anfrage) return
      const daten = res.ok ? ((await res.json()) as { treffer?: Treffer[] }) : { treffer: [] }
      this.treffer = daten.treffer ?? []
      this.hinweis = this.treffer.length ? '' : 'Keine Treffer.'
    },

    async verknuepfen(id: string) {
      const art = this.dialog
      if (art !== 'vorlage' && art !== 'dokument') return
      this.dialog = ''
      if (ziel === 'fokus') await notizenSichern()
      const werte: Record<string, string> = { action: art, item_id: item, ziel }
      werte[art === 'vorlage' ? 'paper_id' : 'motion_id'] = id
      void window.htmx.ajax('post', urls().aktion, {
        target: ziel === 'fokus' ? '#fs-fokus' : '#fs-unterlagen',
        swap: 'outerHTML',
        values: werte,
      })
    },
  }
})

// ---- Beschluss und Anwesenheit ---------------------------------------------------------------------

type Stimme = 'ja' | 'nein' | 'enthaltung'

export const beschlussLeiste = defineComponent(() => ({
  offen: false,
  stimmen: { ja: 0, nein: 0, enthaltung: 0 } as Record<Stimme, number>,

  init() {
    const d = this.$el.dataset
    this.stimmen = { ja: Number(d.ja) || 0, nein: Number(d.nein) || 0, enthaltung: Number(d.enthaltung) || 0 }
  },

  plus(feld: Stimme) {
    this.stimmen[feld] = Math.min(999, (this.stimmen[feld] || 0) + 1)
  },

  minus(feld: Stimme) {
    this.stimmen[feld] = Math.max(0, (this.stimmen[feld] || 0) - 1)
  },
}))

/** Nach dem Austausch der Anwesenheit den Fokus auf dasselbe Bedienelement zurücksetzen (Tastaturbedienung) */
let fokusNachAustausch = ''

export const anwesenheitsListe = defineComponent(() => ({
  init() {
    if (!fokusNachAustausch) return
    const ziel = this.$el.querySelector<HTMLElement>(fokusNachAustausch)
    fokusNachAustausch = ''
    ziel?.focus({ preventScroll: true })
  },

  senden(werte: Record<string, string>) {
    const aktiv = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const id = aktiv?.dataset.id
    if (id) {
      const art = aktiv?.dataset.art ? `[data-art="${CSS.escape(aktiv.dataset.art)}"]` : ''
      const feld = aktiv?.dataset.feld ? `[data-feld="${CSS.escape(aktiv.dataset.feld)}"]` : ''
      fokusNachAustausch = `${aktiv?.tagName.toLowerCase()}[data-id="${CSS.escape(id)}"]${art}${feld}`
    } else if (aktiv?.dataset.feld) {
      fokusNachAustausch = `select[data-feld="${CSS.escape(aktiv.dataset.feld)}"]`
    }
    void window.htmx.ajax('post', urls().aktion, { target: '#fs-anwesenheit', swap: 'outerHTML', values: werte })
  },

  anwesend(el: HTMLInputElement) {
    this.senden({
      action: 'anwesenheit',
      attendance_id: el.dataset.id ?? '',
      status: el.checked ? 'present' : 'absent',
    })
  },

  grund(el: HTMLSelectElement) {
    this.senden({ action: 'anwesenheit', attendance_id: el.dataset.id ?? '', status: el.value })
  },

  art(el: HTMLElement) {
    this.senden({ action: 'anwesenheit', attendance_id: el.dataset.id ?? '', participation_type: el.dataset.art ?? '' })
  },

  rolle(el: HTMLSelectElement) {
    const feld = el.dataset.feld ?? ''
    if (!feld) return
    this.senden({ action: 'rollen', [feld]: el.value })
  },
}))

// ---- Zuständig: Personen und Gruppen -----------------------------------------------------------------

interface Option {
  id: string
  name: string
  art?: string
  anzahl?: number
}

interface Auswahl {
  id: string
  name: string
  anzeige: string
  gruppe: boolean
}

interface ZustaendigConfig {
  personen: Option[]
  gruppen: Option[]
}

export const zustaendigAuswahl = defineComponent(() => {
  const config = readJsonScript<ZustaendigConfig>('fs-zustaendig-config') ?? { personen: [], gruppen: [] }
  const passt = (name: string, suche: string) => !suche || name.toLocaleLowerCase('de').includes(suche)
  return {
    suche: '',
    offen: false,
    aktiv: -1,
    wahl: [] as Auswahl[],

    gefilterteGruppen(): Option[] {
      const q = this.suche.trim().toLocaleLowerCase('de')
      return config.gruppen.filter((g) => passt(g.name, q))
    },

    gefiltertePersonen(): Option[] {
      const q = this.suche.trim().toLocaleLowerCase('de')
      return config.personen.filter((p) => passt(p.name, q))
    },

    alle(): Option[] {
      return [...this.gefilterteGruppen(), ...this.gefiltertePersonen()]
    },

    istGewaehlt(id: string): boolean {
      return this.wahl.some((w) => w.id === id)
    },

    umschalten(o: Option) {
      if (this.istGewaehlt(o.id)) {
        this.entferne(o.id)
        return
      }
      const gruppe = config.gruppen.some((g) => g.id === o.id)
      this.wahl.push({ id: o.id, name: o.name, gruppe, anzeige: gruppe ? `${o.name} (${o.anzahl ?? 0})` : o.name })
      this.suche = ''
    },

    entferne(id: string) {
      this.wahl = this.wahl.filter((w) => w.id !== id)
    },

    oeffne() {
      this.offen = true
      if (this.aktiv >= this.alle().length) this.aktiv = -1
    },

    schliessen() {
      this.offen = false
      this.aktiv = -1
    },

    taste(event: KeyboardEvent) {
      const optionen = this.alle()
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        this.offen = true
        const schritt = event.key === 'ArrowDown' ? 1 : -1
        this.aktiv = optionen.length ? (this.aktiv + schritt + optionen.length) % optionen.length : -1
      } else if (event.key === 'Enter' && this.offen && this.aktiv >= 0 && optionen[this.aktiv]) {
        event.preventDefault()
        this.umschalten(optionen[this.aktiv])
      } else if (event.key === 'Escape' && this.offen) {
        event.stopPropagation()
        this.schliessen()
      } else if (event.key === 'Backspace' && !this.suche && this.wahl.length) {
        this.wahl = this.wahl.slice(0, -1)
      }
    },
  }
})
