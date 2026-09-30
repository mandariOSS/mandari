# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datendrehscheibe (Schicht 3 von 4, ``docs/adr/20260929-schichtenmodell.md``).

Zwischen den Fachmodulen (Session, Work, Bürgerportal) und der Plattform liegt die gemeinsame,
fachliche Integrationsschicht: Verträge für Ereignisse und Befehle, das kanonische RIS-Modell,
Sichten, Adapter und die offene Schnittstelle. Abhängigkeiten zeigen nur nach unten: Die
Drehscheibe darf die Plattform (``apps.events``, ``apps.common``, ``apps.accounts``,
``apps.tenants``) nutzen, aber kein Fachmodul importieren.

Unterpakete:

- ``hub.api``: offene Schnittstelle (OParl 1.1 mit kompatiblen Erweiterungen) – der Aggregator über den
  RIS-Bestand und die eine Serialisierung, die auch die Schnittstelle der Session-Mandanten ausgibt
- ``hub.commands``: Befehle an den Eigentümer der Daten (Dispatcher, Quittung, Idempotenz, Clients)
- ``hub.contracts``: Vertragsregister (Ereignishülle, Schemas je Typ und Version)
- ``hub.ris``: kanonisches RIS-Modell (OParl 1.1 mit gekennzeichneten Erweiterungen), die Abbildungen
  der Quellen darauf (``hub.ris.mapping``) und die Lese-Fassade des RIS-Bestands für die Fachmodule
  (``hub.ris.selectors``)
"""
