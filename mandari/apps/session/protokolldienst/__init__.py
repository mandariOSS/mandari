# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbindung an einen Protokolldienst: Aufnahme, Transkript und Entwurf der Niederschrift laufen in einem eigenen Dienst,
Session meldet den Ablauf der Sitzung und nimmt Entwürfe als Vorschlag zurück (``docs/PROTOKOLLDIENST.md``).

- ``einstellungen``: Anbindung je Mandant (Adresse, API-Schlüssel verschlüsselt, Rückgabeschlüssel als Hash), Prüfung
  der Adresse, OParl-Kennungen, über die der Dienst Sitzung und TOPs zuordnet.
- ``meldungen``: Das Cockpit meldet TOP-Aufruf, Unterbrechung und Abstimmungsergebnis. Die Meldung entsteht in der
  Transaktion der Aktion (Postausgang ``SessionProtokolldienstMeldung``), zugestellt wird nach dem Commit im
  Hintergrund. Nur öffentliche Angaben; nichtöffentliche TOPs und die Einwohnerfragestunde erscheinen als Pause.
- ``client``: Aufrufe der Ereignis-Schnittstelle des Dienstes (Zeitgrenzen, nur geprüfte Ziele, feste Fehlercodes).
- ``auftraege``: Auftrag ``zustellen`` je Sitzung mit Wiederholung; blockiert nie das Cockpit.
- ``vorschlaege``: Befehl ``session.minutes_draft.propose`` (Vorschläge je TOP) und Übernehmen bzw. Verwerfen durch
  die Protokollführung.
- ``api``: Bereich ``protokolldienst`` der Schnittstelle unter ``/api/mandari/v1/protokolldienst/``.
"""
