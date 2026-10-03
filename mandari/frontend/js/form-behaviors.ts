/**
 * Formularhelfer als Datenattribute statt Inline-Skripten in Seiten-Templates (#172).
 *
 * Alle Helfer hängen einmal delegierte Listener an `document`; sie funktionieren damit auch
 * in Fragmenten, die HTMX nachlädt, und brauchen weder `unsafe-inline` noch `unsafe-eval`.
 *
 * - `data-slug-target="<id>"` am Namensfeld: füllt das Feld `<id>` mit einem URL-Kürzel,
 *   solange dort niemand selbst getippt hat (`templates/work/motions/settings/type_form.html`).
 * - `data-filter-options="<Selektor>"` und `data-filter-attr="org"` an einer Auswahl: blendet
 *   im Ziel-Select alle Optionen aus, deren `data-org` den gewählten Wert nicht enthält – mehrere
 *   Werte durch Leerzeichen getrennt, z. B. alle Gremien einer gemeinsamen Sitzung
 *   (`templates/session/papers/consultation_section.html`).
 * - `[data-textblock-picker]` mit `.tb-select` und `.tb-insert`: fügt einen Textbaustein in die
 *   Ziel-Textarea (`data-tb-target`) bzw. die zuletzt fokussierte Textarea ein, Platzhalter
 *   `{datum}`, `{gremium}`, `{sitzung}`, `{vorlage}` (`templates/session/partials/textblock_picker.html`).
 * - `data-browser-info="<Selektor>"` an einem Formular: schreibt Browser- und Umgebungsangaben für
 *   die Fehlersuche in das Feld `<Selektor>` und zeigt sie in `[data-browser-info-preview]`
 *   (`templates/feedback/report.html`).
 * - `data-public-default="<Selektor>"` an einer Gremienauswahl: setzt das Kontrollkästchen `<Selektor>` auf die
 *   Öffentlichkeit der gewählten Option (`data-public="1|0"`) und sperrt es bei `data-public-locked`, z. B. für
 *   den stets nichtöffentlichen Hauptausschuss; mit `data-date-public-default="<Selektor>"` zusätzlich
 *   „Termin veröffentlichen“ nach `data-date-public` (`templates/session/meetings/form.html`, Issue #757).
 */

const UMLAUTE: Record<string, string> = { ä: 'ae', ö: 'oe', ü: 'ue', ß: 'ss' }

/** URL-Kürzel aus einem Namen: klein, Umlaute umschreiben, alles andere zu Bindestrichen. */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[äöüß]/g, (zeichen) => UMLAUTE[zeichen] ?? zeichen)
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
}

export interface TextblockValues {
  datum: string
  gremium: string
  sitzung: string
  vorlage: string
}

/** Platzhalter eines Textbausteins ersetzen. */
export function fillTextblock(text: string, values: TextblockValues): string {
  return text.replace(
    /\{(datum|gremium|sitzung|vorlage)\}/g,
    (_platzhalter, name: keyof TextblockValues) => values[name],
  )
}

export interface InsertResult {
  value: string
  cursor: number
}

/** Text an der Auswahl einfügen; zwischen Wort und Baustein kommt ein Leerzeichen. */
export function insertAtSelection(current: string, start: number, end: number, text: string): InsertResult {
  const prefix = current.slice(0, start)
  const sep = prefix && !prefix.endsWith('\n') && !prefix.endsWith(' ') ? ' ' : ''
  return { value: prefix + sep + text + current.slice(end), cursor: (prefix + sep + text).length }
}

// ---- URL-Kürzel -----------------------------------------------------------------------------

function onSlugInput(event: Event): void {
  const el = event.target
  if (!(el instanceof HTMLInputElement)) return
  const targetId = el.dataset.slugTarget
  if (targetId) {
    const slug = document.getElementById(targetId)
    if (slug instanceof HTMLInputElement && !slug.dataset.slugModified) slug.value = slugify(el.value)
    return
  }
  if (el.dataset.slugField !== undefined) el.dataset.slugModified = 'true'
}

// ---- Abhängige Auswahl ----------------------------------------------------------------------

export function filterOptions(source: HTMLSelectElement): void {
  const selector = source.dataset.filterOptions
  const target = selector ? document.querySelector(selector) : null
  if (!(target instanceof HTMLSelectElement)) return
  const attr = source.dataset.filterAttr ?? 'filter'
  const value = source.value
  for (const option of Array.from(target.options)) {
    if (!option.value) continue
    const values = (option.getAttribute(`data-${attr}`) ?? '').split(/\s+/)
    const visible = !value || values.includes(value)
    option.hidden = !visible
    if (!visible && option.selected) target.value = ''
  }
}

function onFilterChange(event: Event): void {
  const el = event.target
  if (el instanceof HTMLSelectElement && el.dataset.filterOptions) filterOptions(el)
}

function applyFilters(root: ParentNode): void {
  for (const select of Array.from(root.querySelectorAll<HTMLSelectElement>('select[data-filter-options]'))) {
    filterOptions(select)
  }
}

// ---- Textbausteine --------------------------------------------------------------------------

let lastTextarea: HTMLTextAreaElement | null = null

function onFocusIn(event: Event): void {
  if (event.target instanceof HTMLTextAreaElement) lastTextarea = event.target
}

function onTextblockClick(event: Event): void {
  const button = event.target instanceof Element ? event.target.closest('.tb-insert') : null
  const picker = button?.closest<HTMLElement>('[data-textblock-picker]')
  if (!picker) return
  const select = picker.querySelector<HTMLSelectElement>('.tb-select')
  if (!select?.value) return

  const reference = document.getElementById('id_reference')
  const text = fillTextblock(select.value, {
    datum: new Date().toLocaleDateString('de-DE'),
    gremium: picker.dataset.tbGremium ?? '',
    sitzung: picker.dataset.tbSitzung ?? '',
    vorlage: reference instanceof HTMLInputElement ? reference.value : '',
  })

  let target: HTMLTextAreaElement | null = null
  if (picker.dataset.tbTarget) {
    const el = document.getElementById(picker.dataset.tbTarget)
    target = el instanceof HTMLTextAreaElement ? el : null
  } else {
    target = lastTextarea?.isConnected ? lastTextarea : document.querySelector('textarea')
  }
  if (!target) return

  const start = target.selectionStart ?? target.value.length
  const end = target.selectionEnd ?? target.value.length
  const result = insertAtSelection(target.value, start, end, text)
  target.value = result.value
  target.focus()
  target.setSelectionRange(result.cursor, result.cursor)
  select.value = ''
}

// ---- Technische Angaben im Fehlerbericht --------------------------------------------------------

export function collectBrowserInfo(page: string): string {
  return [
    `Browser: ${navigator.userAgent}`,
    `Sprache: ${navigator.language || '-'}`,
    `Plattform: ${navigator.platform || '-'}`,
    `Bildschirm: ${screen.width}x${screen.height} (Fenster ${window.innerWidth}x${window.innerHeight})`,
    `Zeitzone: ${Intl.DateTimeFormat().resolvedOptions().timeZone || '-'}`,
    `Cookies aktiv: ${navigator.cookieEnabled}`,
    `Seite: ${page || document.referrer || '-'}`,
    `Gemeldet am: ${new Date().toISOString()}`,
  ].join('\n')
}

function fillBrowserInfo(root: ParentNode): void {
  for (const form of Array.from(root.querySelectorAll<HTMLFormElement>('form[data-browser-info]'))) {
    const field = form.querySelector(form.dataset.browserInfo ?? '')
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) continue
    const page = form.querySelector<HTMLInputElement>("input[name='url']")?.value ?? ''
    field.value = collectBrowserInfo(page)
    const preview = form.querySelector('[data-browser-info-preview]')
    if (preview) preview.textContent = field.value
  }
}

// ---- Öffentlichkeit nach Gremium ------------------------------------------------------------

export interface PublicDefault {
  isPublic: boolean
  locked: boolean
  datePublic: boolean
}

/** Vorgabe der Öffentlichkeit aus einer Gremien-Option; `null`, wenn die Option keine Angabe trägt. */
export function publicDefault(option: HTMLOptionElement | null | undefined): PublicDefault | null {
  if (!option || option.dataset.public === undefined) return null
  return {
    isPublic: option.dataset.public === '1',
    locked: option.dataset.publicLocked === '1',
    datePublic: option.dataset.datePublic === '1',
  }
}

function onPublicDefaultChange(event: Event): void {
  const select = event.target
  if (!(select instanceof HTMLSelectElement) || !select.dataset.publicDefault) return
  const checkbox = document.querySelector(select.dataset.publicDefault)
  const vorgabe = publicDefault(select.selectedOptions[0])
  if (!(checkbox instanceof HTMLInputElement) || !vorgabe) return
  checkbox.checked = vorgabe.isPublic && !vorgabe.locked
  checkbox.disabled = vorgabe.locked
  const termin = select.dataset.datePublicDefault ? document.querySelector(select.dataset.datePublicDefault) : null
  if (termin instanceof HTMLInputElement) termin.checked = vorgabe.datePublic && !checkbox.checked
}

// ---- Einstieg -------------------------------------------------------------------------------

export function installFormBehaviors(root: Document = document): void {
  root.addEventListener('input', onSlugInput)
  root.addEventListener('change', onFilterChange)
  root.addEventListener('change', onPublicDefaultChange)
  root.addEventListener('focusin', onFocusIn)
  root.addEventListener('click', onTextblockClick)
  // Vite-Module laufen nach dem Parsen: das Markup der Seite steht bereits
  applyFilters(root)
  fillBrowserInfo(root)
  root.body?.addEventListener('htmx:afterSettle', (event) => {
    if (event.target instanceof Element) applyFilters(event.target)
  })
}
