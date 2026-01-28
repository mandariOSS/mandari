# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session - Ratsinformationssystem für Verwaltungen.

Das Session-Modul bietet ein vollständiges RIS für kommunale Verwaltungen:
- Sitzungsmanagement
- Vorlagenverwaltung
- Protokollerstellung
- Anwesenheitsverfolgung
- Sitzungsgeldabrechnung
- OParl-API + erweiterte Session-API

Session ist Multi-Tenant-fähig mit vollständiger Datenisolierung
und Verschlüsselung für sensible Inhalte.
"""

default_app_config = "apps.session.apps.SessionConfig"
