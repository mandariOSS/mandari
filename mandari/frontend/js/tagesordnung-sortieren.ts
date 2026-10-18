/**
 * Tagesordnung einer Sitzung per Ziehen umsortieren (HTML5 Drag and Drop, je Teil), Sitzungsdienst im neuen Rahmen
 * (Issue #944). Im bisherigen Rahmen erledigt das ein Inline-Skript in templates/session/meetings/detail.html; die neue
 * Seite (templates/session/neu/sitzung.html) braucht keins mehr.
 *
 * Markup: ein Element mit `data-tagesordnung-reihenfolge="<URL>"` umschließt die Teile (`data-agenda-section`), deren
 * Zeilen `data-agenda-item="<id>"` tragen und ziehbar sind. Nach dem Ablegen geht die neue Reihenfolge aller Zeilen als
 * `order=<id>,<id>,…` an die URL (POST), danach lädt die Seite neu. Ohne Maus bleiben die Knöpfe „nach oben/unten“.
 */

import { csrfToken } from './csrf'

/** Reihenfolge der Zeilen in Dokumentreihenfolge über alle Teile. */
export function reihenfolge(wurzel: ParentNode): string[] {
  return Array.from(wurzel.querySelectorAll<HTMLElement>('[data-agenda-section] [data-agenda-item]'))
    .map((zeile) => zeile.dataset.agendaItem ?? '')
    .filter((id) => id !== '')
}

function zeileVon(ziel: EventTarget | null): HTMLElement | null {
  return ziel instanceof Element ? (ziel.closest('[data-agenda-item]') as HTMLElement | null) : null
}

async function speichern(url: string, wurzel: ParentNode): Promise<void> {
  const body = new URLSearchParams({ order: reihenfolge(wurzel).join(',') })
  await fetch(url, {
    method: 'POST',
    headers: {
      'X-CSRFToken': csrfToken(),
      'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: body.toString(),
  })
  window.location.reload()
}

function einrichten(wurzel: HTMLElement): void {
  const url = wurzel.dataset.tagesordnungReihenfolge
  if (!url) return
  let gezogen: HTMLElement | null = null
  for (const teil of Array.from(wurzel.querySelectorAll<HTMLElement>('[data-agenda-section]'))) {
    teil.addEventListener('dragstart', (event) => {
      gezogen = zeileVon(event.target)
      if (gezogen && event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
    })
    teil.addEventListener('dragover', (event) => {
      const zeile = zeileVon(event.target)
      if (!zeile || !gezogen || zeile === gezogen || zeile.parentElement !== gezogen.parentElement) return
      event.preventDefault()
      const rechteck = zeile.getBoundingClientRect()
      const danach = event.clientY - rechteck.top > rechteck.height / 2
      zeile.parentElement?.insertBefore(gezogen, danach ? zeile.nextSibling : zeile)
    })
    teil.addEventListener('drop', (event) => {
      event.preventDefault()
      if (gezogen) void speichern(url, wurzel)
    })
    teil.addEventListener('dragend', () => {
      gezogen = null
    })
  }
}

/** Alle Tagesordnungen der Seite einrichten (einmal beim Laden). */
export function installTagesordnungSortieren(root: ParentNode = document): void {
  for (const wurzel of Array.from(root.querySelectorAll<HTMLElement>('[data-tagesordnung-reihenfolge]'))) {
    einrichten(wurzel)
  }
}
