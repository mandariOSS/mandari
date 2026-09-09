/**
 * Aufgaben-Kanban (Alpine-Komponenten `kanbanBoard`, `dropZone`, `labelPicker`).
 *
 * Drag & Drop der Karten über SortableJS (Vendor-Skript `static/vendor/sortablejs`, global
 * `window.Sortable`), Slide-Over-Panel per HTMX, Schnellanlage-Formulare, Anhang-Upload per
 * Drop-Zone und Label-Anlage im Panel. Die URLs kommen aus dem View (`board_config`) per
 * `{{ board_config|json_script:"task-board-config" }}`.
 *
 * Markup: `templates/work/tasks/list.html`, `_panel*.html` und `work/tasks/partials/`.
 */

import { defineComponent } from '../js/alpine/component'
import { confirmAction } from '../js/alpine/confirm-dialog'
import { showToast } from '../js/alpine/toast'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

export const TASK_BOARD_CONFIG_ID = 'task-board-config'
/** Platzhalter-UUID in der Panel-URL, wird clientseitig durch die Aufgaben-ID ersetzt */
const TASK_PLACEHOLDER = '00000000-0000-0000-0000-000000000000'
const PANEL_TARGET = '#task-panel-container'
const STATUSES = ['todo', 'in_progress', 'done'] as const
const STATUS_LABELS: Record<string, string> = { todo: 'Zu erledigen', in_progress: 'In Bearbeitung', done: 'Erledigt' }
const DRAG_RING_CLASSES = ['ring-2', 'ring-primary-300', 'ring-dashed']
/** Wird vom Label-Picker ausgelöst, damit das Board das offene Panel neu lädt */
export const PANEL_RELOAD_EVENT = 'task-panel-reload'

export interface TaskBoardConfig {
  autoOpenTaskId: string
  urls: {
    api: string
    panel: string
    labels: string
    importProtocol: string
    importFile: string
  }
}

// ---- SortableJS (Vendor-Skript ohne Typpaket) ---------------------------------

interface SortableEvent {
  item: HTMLElement
  to: HTMLElement
  newIndex?: number
}

interface SortableOptions {
  group: string
  animation: number
  ghostClass: string
  dragClass: string
  draggable: string
  onStart: () => void
  onEnd: (event: SortableEvent) => void
}

declare global {
  interface Window {
    Sortable: new (el: HTMLElement, options: SortableOptions) => unknown
  }
}

export function readTaskBoardConfig(): TaskBoardConfig {
  const config = readJsonScript<TaskBoardConfig>(TASK_BOARD_CONFIG_ID)
  if (!config) throw new Error(`Aufgaben-Konfiguration #${TASK_BOARD_CONFIG_ID} fehlt`)
  return config
}

function dispatchToast(message: string, type: 'success' | 'error'): void {
  showToast(message, type)
}

/** Alpine-Komponente: `x-data="kanbanBoard"` */
export const kanbanBoard = defineComponent(() => {
  const config = readTaskBoardConfig()

  return {
    panelOpen: false,
    currentTaskId: null as string | null,

    init(): void {
      this.initSortable()

      // Panel öffnen per Custom-Event aus den Karten ($dispatch('open-panel'))
      this.$el.addEventListener('open-panel', (event) => {
        this.openPanel((event as CustomEvent<{ id: string }>).detail.id)
      })

      // Schnellanlage: nach erfolgreichem Request Formular leeren, Platzhalter prüfen
      this.$el.addEventListener('htmx:afterRequest', (event) => {
        const form = event.target
        if (!(form instanceof HTMLFormElement) || !form.matches('[data-quick-add]')) return
        if ((event as CustomEvent<{ successful?: boolean }>).detail?.successful) {
          form.reset()
          this.updatePlaceholders()
        }
      })

      // Aufgabe gelöscht (HX-Trigger) → Panel schließen, Platzhalter aktualisieren
      document.body.addEventListener('taskDeleted', () => {
        this.closePanel()
        this.$nextTick(() => this.updatePlaceholders())
      })

      window.addEventListener(PANEL_RELOAD_EVENT, () => this.reloadPanel())

      // Panel automatisch öffnen (?open=<task-id>)
      if (config.autoOpenTaskId) {
        this.$nextTick(() => this.openPanel(config.autoOpenTaskId))
      }
    },

    panelUrl(taskId: string): string {
      return config.urls.panel.replace(TASK_PLACEHOLDER, taskId)
    },

    openPanel(taskId: string): void {
      this.currentTaskId = taskId
      this.panelOpen = true
      void window.htmx.ajax('get', this.panelUrl(taskId), { target: PANEL_TARGET, swap: 'innerHTML' })
    },

    reloadPanel(): void {
      if (!this.currentTaskId) return
      void window.htmx.ajax('get', this.panelUrl(this.currentTaskId), { target: PANEL_TARGET, swap: 'innerHTML' })
    },

    closePanel(): void {
      this.panelOpen = false
      this.currentTaskId = null
    },

    /** Link zur Aufgabe (?open=<id>) in die Zwischenablage kopieren */
    copyTaskLink(path: string): void {
      void navigator.clipboard.writeText(window.location.origin + path)
      dispatchToast('Link kopiert', 'success')
    },

    /** Aufgabe nach Bestätigung löschen (Panel-Aktion `delete`) */
    deleteTask(actionUrl: string): void {
      void confirmAction({
        title: 'Aufgabe löschen',
        message: 'Die Aufgabe wird unwiderruflich gelöscht.',
        confirmText: 'Löschen',
        variant: 'danger',
      }).then((ok) => {
        if (ok) void window.htmx.ajax('post', actionUrl, { values: { action: 'delete' }, swap: 'none' })
      })
    },

    updatePlaceholders(): void {
      for (const status of STATUSES) {
        const column = document.getElementById(`column-${status}`)
        const placeholder = column?.querySelector('.empty-placeholder')
        if (!column || !placeholder) continue
        placeholder.classList.toggle('hidden', column.querySelectorAll('.task-card').length > 0)
      }
    },

    initSortable(): void {
      const columns = () => document.querySelectorAll<HTMLElement>('[data-status]')
      for (const status of STATUSES) {
        const el = document.getElementById(`column-${status}`)
        if (!el) continue
        new window.Sortable(el, {
          group: 'tasks',
          animation: 150,
          ghostClass: 'opacity-50',
          dragClass: 'shadow-lg',
          draggable: '.task-card',
          onStart: () => {
            for (const column of columns()) column.classList.add(...DRAG_RING_CLASSES)
          },
          onEnd: (event) => {
            for (const column of columns()) column.classList.remove(...DRAG_RING_CLASSES)
            this.updatePlaceholders()
            void this.moveTask(event.item.dataset.id ?? '', event.to.dataset.status ?? '', event.newIndex ?? 0)
          },
        })
      }
    },

    async moveTask(taskId: string, status: string, position: number): Promise<void> {
      try {
        const response = await fetch(config.urls.api, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify({ action: 'move', task_id: taskId, status, position }),
        })
        if (response.ok) {
          dispatchToast(`Verschoben nach "${STATUS_LABELS[status] || status}"`, 'success')
        } else {
          console.error('[Kanban] Move failed:', response.status)
          dispatchToast('Verschieben fehlgeschlagen.', 'error')
          location.reload()
        }
      } catch (err) {
        console.error('[Kanban] Move error:', err)
        dispatchToast('Verschieben fehlgeschlagen.', 'error')
        location.reload()
      }
    },
  }
})

/** Alpine-Komponente: `x-data="dropZone"` (Anhang-Upload im Panel) */
export const dropZone = defineComponent(() => ({
  dragging: false,

  submitForm(event: Event): void {
    const form = (event.target as HTMLElement).closest('div')?.querySelector('form')
    if (form) window.htmx.trigger(form, 'submit')
  },

  handleDrop(event: DragEvent): void {
    this.dragging = false
    const files = event.dataTransfer?.files
    if (!files || files.length === 0) return
    const container = (event.target as HTMLElement).closest('div')
    const input = container?.querySelector<HTMLInputElement>('input[type=file]')
    if (!input) return
    const dataTransfer = new DataTransfer()
    dataTransfer.items.add(files[0])
    input.files = dataTransfer.files
    const form = container?.querySelector('form')
    if (form) window.htmx.trigger(form, 'submit')
  },
}))

/** Alpine-Komponente: `x-data="labelPicker"` (Labels im Panel) */
export const labelPicker = defineComponent(() => ({
  open: false,

  async createLabel(event: Event): Promise<void> {
    const form = event.target as HTMLFormElement
    const url = form.getAttribute('hx-post') || form.action
    try {
      const response = await fetch(url, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-CSRFToken': csrfToken() },
      })
      if (response.ok) {
        form.reset()
        // Panel neu laden, damit das neue Label erscheint
        window.dispatchEvent(new CustomEvent(PANEL_RELOAD_EVENT))
      }
    } catch (err) {
      console.error('[Labels] Create error:', err)
    }
  },
}))
