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

// ---- Satz A (#174): Dokumentenliste, Aufgaben-Kanban, Konto-Sicherheit ----------
import { documentManager } from '../alpine/document-manager'
import { securitySettings } from '../alpine/security-settings'
import { dropZone, kanbanBoard, labelPicker } from '../alpine/task-board'
import { fileImportManager, importManager } from '../alpine/task-import'

Alpine.data('documentManager', documentManager)
Alpine.data('kanbanBoard', kanbanBoard)
Alpine.data('dropZone', dropZone)
Alpine.data('labelPicker', labelPicker)
Alpine.data('importManager', importManager)
Alpine.data('fileImportManager', fileImportManager)
Alpine.data('securitySettings', securitySettings)

// ---- Satz B (#174): Fraktionssitzung, TOP-Panel, Fraktions-Einstellungen ----------
import { agendaItemPanel } from '../alpine/agenda-item-panel'
import { factionDetail } from '../alpine/faction-detail'
import { factionTitlePreview } from '../alpine/faction-title-preview'

Alpine.data('agendaItemPanel', agendaItemPanel)
Alpine.data('factionDetail', factionDetail)
Alpine.data('factionTitlePreview', factionTitlePreview)
