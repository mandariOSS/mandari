/**
 * Typhilfe für Alpine-Fabriken.
 *
 * `defineComponent(() => ({ … }))` typisiert `this` in den Methoden der Komponente als
 * eigene Felder plus Alpine-Magics (`$el`, `$refs`, `$watch`, `$nextTick`, `$dispatch`),
 * ohne dass ein separates Interface für jedes Feld gepflegt werden muss.
 * Registrierung wie gehabt: `Alpine.data('name', factory)`.
 */

import type { AlpineComponent } from 'alpinejs'

export function defineComponent<T>(factory: () => AlpineComponent<T>): () => AlpineComponent<T> {
  return factory
}
