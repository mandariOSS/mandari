/**
 * Slash Commands Extension — /-Menü for quick block insertion.
 *
 * Typing "/" at the start of a line (or after whitespace) opens a popup
 * with available block types. Uses @tiptap/suggestion under the hood.
 */

import { Extension } from '@tiptap/core'
import Suggestion from '@tiptap/suggestion'
import type { SuggestionOptions, SuggestionProps, SuggestionKeyDownProps } from '@tiptap/suggestion'

interface SlashCommandItem {
  title: string
  description: string
  icon: string
  command: (props: { editor: any; range: any }) => void
}

function getCommandItems(): SlashCommandItem[] {
  return [
    {
      title: 'Überschrift 1',
      description: 'Große Überschrift',
      icon: 'heading-1',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).setNode('heading', { level: 1 }).run()
      },
    },
    {
      title: 'Überschrift 2',
      description: 'Mittlere Überschrift',
      icon: 'heading-2',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).setNode('heading', { level: 2 }).run()
      },
    },
    {
      title: 'Überschrift 3',
      description: 'Kleine Überschrift',
      icon: 'heading-3',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).setNode('heading', { level: 3 }).run()
      },
    },
    {
      title: 'Aufzählung',
      description: 'Ungeordnete Liste',
      icon: 'list',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).toggleBulletList().run()
      },
    },
    {
      title: 'Nummerierung',
      description: 'Geordnete Liste',
      icon: 'list-ordered',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).toggleOrderedList().run()
      },
    },
    {
      title: 'Aufgabenliste',
      description: 'Checkliste mit Häkchen',
      icon: 'list-checks',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).toggleTaskList().run()
      },
    },
    {
      title: 'Tabelle',
      description: '3×3 Tabelle einfügen',
      icon: 'table',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()
      },
    },
    {
      title: 'Zitat',
      description: 'Blockzitat',
      icon: 'quote',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).toggleBlockquote().run()
      },
    },
    {
      title: 'Trennlinie',
      description: 'Horizontale Linie',
      icon: 'minus',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).setHorizontalRule().run()
      },
    },
    {
      title: 'Seitenumbruch',
      description: 'Neue Seite beginnen',
      icon: 'file-output',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).setPageBreak().run()
      },
    },
    {
      title: 'Bild',
      description: 'Bild per URL einfügen',
      icon: 'image',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).run()
        // Dispatch custom event for the Alpine.js handler
        window.dispatchEvent(new CustomEvent('slash-insert-image'))
      },
    },
    {
      title: 'Code',
      description: 'Code-Block',
      icon: 'code',
      command: ({ editor, range }) => {
        editor.chain().focus().deleteRange(range).toggleCodeBlock().run()
      },
    },
  ]
}

/** Create and manage the slash commands popup DOM element. */
function createPopupRenderer() {
  let popup: HTMLElement | null = null
  let items: SlashCommandItem[] = []
  let selectedIndex = 0
  let commandFn: ((item: SlashCommandItem) => void) | null = null

  function render() {
    if (!popup) return
    popup.innerHTML = ''
    items.forEach((item, index) => {
      const el = document.createElement('button')
      el.type = 'button'
      el.className = 'slash-cmd-item' + (index === selectedIndex ? ' slash-cmd-item--active' : '')
      el.innerHTML = `
        <span class="slash-cmd-icon"><i data-lucide="${item.icon}"></i></span>
        <span class="slash-cmd-text">
          <span class="slash-cmd-title">${item.title}</span>
          <span class="slash-cmd-desc">${item.description}</span>
        </span>
      `
      el.addEventListener('mousedown', (e) => {
        e.preventDefault()
        commandFn?.(item)
      })
      el.addEventListener('mouseenter', () => {
        selectedIndex = index
        render()
      })
      popup!.appendChild(el)
    })

    // Initialize lucide icons inside popup
    if (typeof (window as any).lucide !== 'undefined') {
      ;(window as any).lucide.createIcons({ nodes: [popup] })
    }
  }

  return {
    onStart(props: SuggestionProps) {
      items = props.items as SlashCommandItem[]
      selectedIndex = 0
      commandFn = (item) => props.command(item)

      popup = document.createElement('div')
      popup.className = 'slash-cmd-popup'
      document.body.appendChild(popup)

      render()
      updatePosition(props)
    },

    onUpdate(props: SuggestionProps) {
      items = props.items as SlashCommandItem[]
      selectedIndex = 0
      commandFn = (item) => props.command(item)
      render()
      updatePosition(props)
    },

    onKeyDown(props: SuggestionKeyDownProps): boolean {
      if (props.event.key === 'ArrowDown') {
        selectedIndex = (selectedIndex + 1) % items.length
        render()
        scrollActiveIntoView()
        return true
      }
      if (props.event.key === 'ArrowUp') {
        selectedIndex = (selectedIndex - 1 + items.length) % items.length
        render()
        scrollActiveIntoView()
        return true
      }
      if (props.event.key === 'Enter') {
        const item = items[selectedIndex]
        if (item) commandFn?.(item)
        return true
      }
      if (props.event.key === 'Escape') {
        destroy()
        return true
      }
      return false
    },

    onExit() {
      destroy()
    },
  }

  function updatePosition(props: SuggestionProps) {
    if (!popup) return
    const rect = props.clientRect?.()
    if (!rect) return
    popup.style.left = `${rect.left}px`
    popup.style.top = `${rect.bottom + 4}px`
    // Flip up if near bottom
    if (rect.bottom + 300 > window.innerHeight) {
      popup.style.top = `${rect.top - popup.offsetHeight - 4}px`
    }
  }

  function scrollActiveIntoView() {
    if (!popup) return
    const active = popup.querySelector('.slash-cmd-item--active')
    if (active) active.scrollIntoView({ block: 'nearest' })
  }

  function destroy() {
    if (popup) {
      popup.remove()
      popup = null
    }
  }
}

export const SlashCommands = Extension.create({
  name: 'slashCommands',

  addOptions() {
    return {
      suggestion: {
        char: '/',
        startOfLine: false,
        command: ({ editor, range, props }: { editor: any; range: any; props: SlashCommandItem }) => {
          props.command({ editor, range })
        },
        items: ({ query }: { query: string }) => {
          const q = query.toLowerCase()
          return getCommandItems().filter(
            (item) =>
              item.title.toLowerCase().includes(q) ||
              item.description.toLowerCase().includes(q)
          )
        },
        render: createPopupRenderer,
      } as Partial<SuggestionOptions>,
    }
  },

  addProseMirrorPlugins() {
    return [
      Suggestion({
        editor: this.editor,
        ...this.options.suggestion,
      }),
    ]
  },
})

export default SlashCommands
