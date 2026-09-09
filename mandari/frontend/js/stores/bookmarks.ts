/**
 * Merkliste (Insight-Portal): Alpine-Store `bookmarks`.
 *
 * Angemeldet: Server-API. Anonym: localStorage. Aktivierung nur, wenn das Layout
 * `data-portal="insight"` am `<html>`-Element setzt.
 */

import type { Alpine as AlpineType } from 'alpinejs'
import { csrfToken } from '../htmx-setup'

export type BookmarkType = 'person' | 'paper' | 'meeting' | 'organization'
type BookmarkData = Record<BookmarkType, string[]>

export interface BookmarksStore {
  _data: BookmarkData
  _loaded: boolean
  init(this: BookmarksStore): void
  has(this: BookmarksStore, type: BookmarkType, id: string | number): boolean
  toggle(this: BookmarksStore, type: BookmarkType, id: string | number): void
  count(this: BookmarksStore): number
  _save(this: BookmarksStore): void
}

const STORAGE_KEY = 'mandari_bookmarks'
const IDS_URL = '/insight/merkliste/api/ids/'
const TOGGLE_URL = '/insight/merkliste/api/toggle/'

function emptyData(): BookmarkData {
  return { person: [], paper: [], meeting: [], organization: [] }
}

export function isAuthenticated(): boolean {
  return document.documentElement.dataset.authenticated === 'true'
}

export function createBookmarksStore(): BookmarksStore {
  return {
    _data: emptyData(),
    _loaded: false,

    init() {
      if (isAuthenticated()) {
        fetch(IDS_URL)
          .then((r) => r.json())
          .then((data: BookmarkData) => {
            this._data = data
            this._loaded = true
          })
          .catch(() => {
            this._loaded = true
          })
        return
      }
      try {
        const saved = localStorage.getItem(STORAGE_KEY)
        if (saved) this._data = JSON.parse(saved) as BookmarkData
      } catch {
        /* localStorage nicht verfügbar */
      }
      this._loaded = true
    },

    has(type, id) {
      const list = this._data[type]
      return Array.isArray(list) && list.includes(String(id))
    },

    toggle(type, id) {
      const idStr = String(id)
      if (!this._data[type]) this._data[type] = []
      const idx = this._data[type].indexOf(idStr)
      if (idx !== -1) this._data[type].splice(idx, 1)
      else this._data[type].push(idStr)

      if (isAuthenticated()) {
        fetch(TOGGLE_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify({ type, id: idStr }),
        }).catch(() => {})
      } else {
        this._save()
      }
    },

    count() {
      return Object.values(this._data).reduce((sum, list) => sum + list.length, 0)
    },

    _save() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(this._data))
      } catch {
        /* localStorage nicht verfügbar */
      }
    },
  }
}

export function registerBookmarksStore(Alpine: AlpineType): void {
  Alpine.store('bookmarks', createBookmarksStore())
}
