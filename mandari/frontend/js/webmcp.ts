/**
 * WebMCP an den lesenden Formularen des Bürgerportals (Issue #1309).
 *
 * Formulare mit `toolname` sind Werkzeuge für KI-Agenten im Browser (`insight_core/templatetags/webmcp.py`). Die
 * Suche schickt mit HTMX ab, der Kommunenwechsel mit Alpine (`@submit.prevent`); beide unterbinden die Navigation.
 * Ruft ein Agent das Werkzeug auf (`SubmitEvent.agentInvoked`), bekäme er so kein Ergebnis. Deshalb schickt das
 * Formular dann gewöhnlich ab: Der Agent landet auf der Ergebnisseite und liest sie (WebMCP: ohne `respondWith()`
 * navigiert das Formular). Ein Lauscher in der Capture-Phase am Dokument hält das Ereignis vor den Handlern am
 * Formular an; die Standardaktion (GET an `action` mit den Feldern) bleibt. Für Menschen ändert sich nichts.
 */

type AgentSubmit = Event & { agentInvoked?: boolean }

/** Kommt das Absenden von einem Agenten und gehört das Formular zu einem WebMCP-Werkzeug? */
export function istAgentAufruf(event: Event): boolean {
  if ((event as AgentSubmit).agentInvoked !== true) return false
  const ziel = event.target as Element | null
  return Boolean(ziel && typeof ziel.hasAttribute === 'function' && ziel.hasAttribute('toolname'))
}

export function installWebmcpFormulare(doc: Pick<Document, 'addEventListener'> = document): void {
  doc.addEventListener(
    'submit',
    (event) => {
      if (istAgentAufruf(event)) event.stopImmediatePropagation()
    },
    true,
  )
}
