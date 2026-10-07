/**
 * CSRF-Token für eigene fetch()-Aufrufe: aus dem Meta-Tag des Layouts, sonst aus dem Cookie.
 */

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

export function csrfToken(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')
  // Über HTTPS trägt das Cookie das Präfix __Host- (siehe settings.py, CSRF_COOKIE_NAME)
  return meta?.content || readCookie('__Host-csrftoken') || readCookie('csrftoken')
}

/**
 * Wie `csrfToken`, aber das Cookie zuerst: Nach einer neuen Anmeldung (z. B. in einem anderen Tab) wechselt Django
 * das Token; das Meta-Tag der offenen Seite ist dann veraltet, das Cookie nicht. Für Wiederholungen beim Speichern.
 */
export function csrfTokenAktuell(): string {
  return readCookie('__Host-csrftoken') || readCookie('csrftoken') || csrfToken()
}
