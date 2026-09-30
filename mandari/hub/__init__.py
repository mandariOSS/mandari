# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datendrehscheibe (Schicht 3 von 4, ``docs/adr/20260929-schichtenmodell.md``).

Zwischen den Fachmodulen (Session, Work, Bürgerportal) und der Plattform liegt die gemeinsame,
fachliche Integrationsschicht: Verträge für Ereignisse und Befehle, das kanonische RIS-Modell,
Sichten, Adapter und die offene Schnittstelle. Abhängigkeiten zeigen nur nach unten: Die
Drehscheibe darf die Plattform (``apps.events``, ``apps.common``, ``apps.accounts``,
``apps.tenants``) nutzen, aber kein Fachmodul importieren.

Unterpakete:

- ``hub.contracts``: Vertragsregister (Ereignishülle, Schemas je Typ und Version)
"""
