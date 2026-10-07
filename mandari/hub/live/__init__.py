# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Übertragungen von Gremiensitzungen (Issue #915, ``docs/adr/20261007-live-uebertragung.md``).

Insight erkennt über die Status-Schnittstelle des Streaming-Anbieters, wann eine Kommune eine Sitzung überträgt,
liest während der Übertragung im Takt ein Einzelbild per Texterkennung (TOP-Nummer, TOP-Titel, Name und Fraktion
aus der Einblendung der Kommune) und führt daraus Tagesordnungsabschnitte und Wortmeldungen. Bild und Ton werden nie
gespeichert; Einzelbilder liegen nur im Arbeitsspeicher. Redezeiten werden weder gelesen noch erfasst.

- ``anbieter``: Adapter je Streaming-Anbieter (Status, HLS-Quelle, Einbettung), Register nach Code.
- ``profil``: Einblendungsprofil als Daten (Lage der Felder, Balkenerkennung, Funktionsbezeichnungen).
- ``einzelbild``, ``ocr``, ``lesung``: Einzelbild aus HLS, Texterkennung, Auswertung nach Profil.
- ``zuordnung``: TOP-Nummer → Tagesordnungspunkt, gelesener Name → Person.
- ``services``: Zustand je Übertragung, Entprellung, Ereignisse der Datendrehscheibe.
- ``selectors``: Lesezugriffe für die Live-Seite in Insight und für Abonnenten.
"""
