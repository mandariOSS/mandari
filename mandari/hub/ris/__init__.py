# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanonisches RIS-Modell und RIS-Bestand in der Datendrehscheibe
(``docs/adr/20260929-kanonisches-modell.md``).

Das Modell folgt OParl 1.1; eigene Erweiterungen tragen einen Namensraum (``mandari:``).

- ``hub.ris.canonical``: Bausteine des Modells – Typ-URLs, Datums- und Zeitformate, gekürzte Objekte
  für Gelöschtes, die Werteliste von ``organizationType``.
- ``hub.ris.mapping``: Abbildungen der Quellen auf das Modell. ``mapping.session`` bildet die Objekte
  von mandari Session ab; die Session-OParl-Schnittstelle gibt genau diese Abbildung aus.
- ``hub.ris.selectors``: Lese-Fassade mit fachlichen Abfragen. Fachmodule lesen Sitzungen,
  Tagesordnungspunkte, Vorlagen, Gremien, Personen und Dateien darüber statt über
  ``OParl*.objects``; ``scripts/check_ris_access_ratchet.py`` zählt die verbliebenen Direktzugriffe.

Die Tabellen bleiben in ``insight_core`` (Label und ``oparl_*``-Tabellen unverändert). Die
Drehscheibe importiert kein Fachmodul (``docs/adr/20260929-schichtenmodell.md``): Was eine Abbildung
vom Fachmodul braucht, bekommt sie vom Aufrufer gereicht.
"""
