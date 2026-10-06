/**
 * Endgültiges Löschen mit ausdrücklicher Eingabe (Alpine-Komponente `deleteConfirmation`, Issue #897).
 *
 * Am Löschformular: `x-data="deleteConfirmation" data-phrases='["12.10.2026", …]'`. Der Knopf „Endgültig löschen“
 * bleibt gesperrt (`x-bind:disabled="!matches"`), bis die Eingabe einer der erwarteten Angaben entspricht – Datum
 * oder Titel der Sitzung, Name der Reihe, Nummer oder Titel des TOPs. Eine leere Liste heißt: keine Eingabe nötig.
 * Der Server prüft die Eingabe beim Absenden noch einmal mit derselben Normalisierung
 * (`apps/work/faction/deletion.py`, `normalize`). Lehnt er ab, steht seine feste Meldung im Dialog (`error`).
 *
 * Markup: `templates/work/faction/partials/_delete_confirmation.html`.
 */

import { defineComponent } from '../js/alpine/component'

/** Eingabe vergleichbar machen: Leerraum zusammenfassen, Kleinschreibung (wie `normalize` im Server) */
export function normalizeConfirmation(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(' ').toLowerCase()
}

/** Erwartete Angaben aus `data-phrases` (JSON-Liste); fehlt das Attribut oder ist es unlesbar: `null` */
export function parsePhrases(raw: string | undefined): string[] | null {
  if (raw === undefined) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : null
  } catch {
    return null
  }
}

const FALLBACK_403 = 'Keine Berechtigung zum Löschen.'
const FALLBACK = 'Löschen nicht möglich. Bitte erneut versuchen.'

/** Feste Meldung des Servers (nur Klartext-Antworten), sonst ein allgemeiner Hinweis */
export function rejectionMessage(xhr: XMLHttpRequest | undefined): string {
  const type = xhr?.getResponseHeader('Content-Type') ?? ''
  const text = type.startsWith('text/plain') ? (xhr?.responseText ?? '').trim() : ''
  if (text) return text
  return xhr?.status === 403 ? FALLBACK_403 : FALLBACK
}

export const deleteConfirmation = defineComponent(() => ({
  value: '',
  error: '',
  /** `null`: Angaben fehlen oder sind unlesbar – dann bleibt der Knopf sicherheitshalber gesperrt */
  phrases: null as string[] | null,

  init() {
    this.phrases = parsePhrases(this.$el.dataset.phrases)
  },

  get matches(): boolean {
    if (this.phrases === null) return false
    if (this.phrases.length === 0) return true
    return this.phrases.includes(normalizeConfirmation(this.value))
  },

  /** Ablehnung durch den Server im Dialog anzeigen (htmx:responseError am Formular) */
  showError(event: Event) {
    const detail = (event as CustomEvent<{ xhr?: XMLHttpRequest }>).detail
    this.error = rejectionMessage(detail?.xhr)
  },
}))
