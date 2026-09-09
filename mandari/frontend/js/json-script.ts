/**
 * Server-Daten aus `{{ data|json_script:"id" }}` lesen.
 *
 * Django rendert `<script type="application/json" id="…">`; der Inhalt ist HTML-sicher
 * kodiert und wird hier geparst. Fehlt das Element oder ist es leer, kommt `null`.
 */
export function readJsonScript<T>(id: string): T | null {
  const el = document.getElementById(id)
  if (!el?.textContent) return null
  try {
    return JSON.parse(el.textContent) as T
  } catch {
    return null
  }
}
