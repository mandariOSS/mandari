/**
 * Work-Portal – Einstieg für seitenbezogene Alpine-Komponenten (Hotspot-Zerlegung, #174).
 *
 * Wird in templates/work/base_work.html geladen. Jede Komponente lebt in
 * frontend/alpine/<name>.ts und wird hier mit Alpine.data() registriert; Templates
 * referenzieren sie per x-data="name" und liefern Daten per json_script.
 * Registrierung auf Top-Level: main.ts startet Alpine erst bei DOMContentLoaded.
 */

import Alpine from 'alpinejs'

// Beispiel: import { taskBoard } from '../alpine/task-board'
// Alpine.data('taskBoard', taskBoard)

export { Alpine }
