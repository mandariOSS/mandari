# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Bestand in der Drehscheibe (``docs/adr/20260929-kanonisches-modell.md``).

- ``hub.ris.selectors``: Lese-Fassade mit fachlichen Abfragen. Fachmodule lesen Sitzungen,
  Tagesordnungspunkte, Vorlagen, Gremien, Personen und Dateien darüber statt über
  ``OParl*.objects``; ``scripts/check_ris_access_ratchet.py`` zählt die verbliebenen Direktzugriffe.

Die Tabellen bleiben in ``insight_core`` (Label und ``oparl_*``-Tabellen unverändert).
"""
