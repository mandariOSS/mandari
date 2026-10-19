/**
 * @-Erwähnen in den Notizen der Fraktionssitzung (Issue #874).
 *
 * „@“ öffnet eine Vorschlagsliste der Mitglieder (Muster `fraktionssitzungen/sitzung.html`, Popup `.fs-erw`).
 * Gewählt wird mit Pfeiltasten und Eingabe oder per Klick; die Erwähnung steht als eigener Knoten im Text:
 * `<span class="erwaehnung" data-type="mention" data-id="<Mitgliedschaft>">@Name</span>`. Der Server bereinigt
 * das HTML (apps/work/sanitize.py) und benachrichtigt neu erwähnte Personen (apps/work/faction/erwaehnungen.py).
 */

import { mergeAttributes, Node } from '@tiptap/core'
import type { SuggestionKeyDownProps, SuggestionProps } from '@tiptap/suggestion'
import Suggestion from '@tiptap/suggestion'

export interface ErwaehnbarePerson {
  id: string
  name: string
  rolle?: string
}

export interface ErwaehnungOptionen {
  personen: ErwaehnbarePerson[]
}

const MAX_TREFFER = 8

function passend(personen: ErwaehnbarePerson[], abfrage: string): ErwaehnbarePerson[] {
  const q = abfrage.trim().toLowerCase()
  const treffer = q
    ? personen.filter(
        (p) =>
          p.name
            .toLowerCase()
            .split(/\s+/)
            .some((teil) => teil.startsWith(q)) || p.name.toLowerCase().includes(q),
      )
    : personen
  return treffer.slice(0, MAX_TREFFER)
}

let zaehler = 0

/** Popup der Vorschläge als Listbox; der Editor bleibt fokussiert (aria-activedescendant). */
function vorschlagsliste() {
  let el: HTMLElement | null = null
  let liste: HTMLUListElement | null = null
  let eintraege: ErwaehnbarePerson[] = []
  let aktiv = 0
  let waehlen: ((p: ErwaehnbarePerson) => void) | null = null
  let editorDom: HTMLElement | null = null
  const kennung = `fs-erw-${++zaehler}`

  function zeichnen() {
    if (!el || !liste) return
    liste.replaceChildren()
    if (!eintraege.length) {
      const leer = document.createElement('li')
      leer.setAttribute('role', 'presentation')
      leer.className = 'erw-leer'
      leer.textContent = 'Niemand gefunden'
      liste.append(leer)
    } else {
      const gruppe = document.createElement('li')
      gruppe.setAttribute('role', 'presentation')
      gruppe.className = 'erw-gruppe'
      gruppe.textContent = 'Personen'
      liste.append(gruppe)
    }
    eintraege.forEach((p, i) => {
      const li = document.createElement('li')
      li.id = `${kennung}-${i}`
      li.setAttribute('role', 'option')
      li.setAttribute('aria-selected', i === aktiv ? 'true' : 'false')
      const name = document.createElement('span')
      name.textContent = p.name
      li.append(name)
      if (p.rolle) {
        const rolle = document.createElement('small')
        rolle.textContent = p.rolle
        li.append(rolle)
      }
      li.addEventListener('mousedown', (e) => {
        e.preventDefault()
        waehlen?.(p)
      })
      li.addEventListener('mouseenter', () => {
        aktiv = i
        markieren()
      })
      liste?.append(li)
    })
    markieren()
  }

  function markieren() {
    if (!liste) return
    for (const li of liste.querySelectorAll<HTMLElement>('[role=option]')) {
      li.setAttribute('aria-selected', li.id === `${kennung}-${aktiv}` ? 'true' : 'false')
    }
    if (eintraege.length) {
      editorDom?.setAttribute('aria-activedescendant', `${kennung}-${aktiv}`)
      document.getElementById(`${kennung}-${aktiv}`)?.scrollIntoView({ block: 'nearest' })
    } else {
      editorDom?.removeAttribute('aria-activedescendant')
    }
  }

  function platzieren(props: SuggestionProps) {
    if (!el) return
    const rect = props.clientRect?.()
    if (!rect) return
    const breite = el.offsetWidth || 320
    el.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - breite - 8))}px`
    const unten = rect.bottom + 6
    const hoehe = Math.min(360, el.offsetHeight)
    el.style.top = unten + hoehe > window.innerHeight - 8 ? `${Math.max(8, rect.top - hoehe - 6)}px` : `${unten}px`
  }

  function schliessen() {
    el?.remove()
    el = null
    liste = null
    editorDom?.removeAttribute('aria-activedescendant')
    editorDom?.setAttribute('aria-expanded', 'false')
  }

  return {
    onStart(props: SuggestionProps) {
      editorDom = props.editor.view.dom as HTMLElement
      eintraege = props.items as ErwaehnbarePerson[]
      aktiv = 0
      waehlen = (p) => props.command(p)
      el = document.createElement('div')
      el.className = 'wf fs-erw'
      liste = document.createElement('ul')
      liste.id = kennung
      liste.setAttribute('role', 'listbox')
      liste.setAttribute('aria-label', 'Erwähnen')
      const fuss = document.createElement('p')
      fuss.className = 'erw-fuss'
      fuss.textContent = 'Erwähnte Personen erhalten eine Benachrichtigung.'
      el.append(liste, fuss)
      document.body.append(el)
      editorDom.setAttribute('aria-controls', kennung)
      editorDom.setAttribute('aria-expanded', 'true')
      zeichnen()
      platzieren(props)
    },
    onUpdate(props: SuggestionProps) {
      eintraege = props.items as ErwaehnbarePerson[]
      aktiv = 0
      waehlen = (p) => props.command(p)
      zeichnen()
      platzieren(props)
    },
    onKeyDown(props: SuggestionKeyDownProps): boolean {
      const taste = props.event.key
      if (taste === 'Escape') {
        schliessen()
        return true
      }
      if (!eintraege.length) return false
      if (taste === 'ArrowDown' || taste === 'ArrowUp') {
        aktiv = (aktiv + (taste === 'ArrowDown' ? 1 : -1) + eintraege.length) % eintraege.length
        markieren()
        return true
      }
      if (taste === 'Enter' || taste === 'Tab') {
        const p = eintraege[aktiv]
        if (p) waehlen?.(p)
        return true
      }
      return false
    },
    onExit() {
      schliessen()
    },
  }
}

export const Erwaehnung = Node.create<ErwaehnungOptionen>({
  name: 'mention',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: false,

  addOptions() {
    return { personen: [] }
  },

  addAttributes() {
    return {
      id: {
        default: null,
        parseHTML: (element) => element.getAttribute('data-id'),
        renderHTML: (attrs) => (attrs.id ? { 'data-id': attrs.id } : {}),
      },
      label: {
        default: '',
        parseHTML: (element) => (element.textContent || '').replace(/^@/, ''),
        renderHTML: () => ({}),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-type="mention"]' }]
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      'span',
      mergeAttributes({ class: 'erwaehnung', 'data-type': 'mention' }, HTMLAttributes),
      `@${node.attrs.label}`,
    ]
  },

  renderText({ node }) {
    return `@${node.attrs.label}`
  },

  addProseMirrorPlugins() {
    const personen = this.options.personen
    return [
      Suggestion<ErwaehnbarePerson, ErwaehnbarePerson>({
        editor: this.editor,
        char: '@',
        allowSpaces: false,
        items: ({ query }) => passend(personen, query),
        command: ({ editor, range, props }) => {
          editor
            .chain()
            .focus()
            .insertContentAt(range, [
              { type: 'mention', attrs: { id: props.id, label: props.name } },
              { type: 'text', text: ' ' },
            ])
            .run()
        },
        render: vorschlagsliste,
      }),
    ]
  },
})

export default Erwaehnung
