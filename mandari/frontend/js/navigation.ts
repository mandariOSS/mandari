/**
 * Sichere Navigation innerhalb der Anwendung.
 *
 * Alle Weiterleitungsziele im Frontend stammen aus dem Server (Django-`reverse()`
 * im `json_script`-Block bzw. im JSON einer API-Antwort). Damit ein späterer Fehler
 * in einer dieser Quellen nicht zu `javascript:`-/`data:`-URLs oder einer
 * Weiterleitung auf eine fremde Domain führen kann, laufen alle Sprünge über diese
 * Stelle: Das Ziel wird gegen die aktuelle Herkunft aufgelöst und nur übernommen,
 * wenn es dieselbe Herkunft mit einem http(s)-Schema hat.
 */

/** Same-Origin-Ziel als absolute URL, sonst `null`. */
export function resolveSameOrigin(target: unknown): string | null {
  if (typeof target !== 'string' || target === '') return null
  let url: URL
  try {
    url = new URL(target, window.location.href)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.origin !== window.location.origin) return null
  return url.href
}

/** Zum Ziel springen, wenn es dieselbe Herkunft hat; sonst zum Ausweichziel. */
export function navigateTo(target: unknown, fallback = '/'): void {
  window.location.assign(resolveSameOrigin(target) ?? resolveSameOrigin(fallback) ?? '/')
}
