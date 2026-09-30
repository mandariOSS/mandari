# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Offene Schnittstelle der Datendrehscheibe: OParl 1.1 mit kompatiblen Erweiterungen.

Zwei Ausgaben, eine Serialisierung (``docs/adr/20260929-kanonisches-modell.md``):

- der **Aggregator** unter ``/oparl/v1/`` über den RIS-Bestand aller Kommunen (``hub.api.aggregator``,
  Routen in ``hub.api.urls``),
- die **Schnittstelle je Session-Mandant** unter ``/session/<slug>/api/oparl/``. Ihre Endpunkte liegen
  im Fachmodul (``apps/session/api/oparl.py``), weil nur Session weiß, was eines Mandanten öffentlich
  ist; die Drehscheibe importiert kein Fachmodul.

Beide gehen denselben Weg::

    Quelle ──Abbildung──▶ kanonisches Objekt ──Serialisierung──▶ Antwort
    RIS-Bestand   hub.ris.mapping.bestand                 hub.api.serialization
    Session       hub.ris.mapping.session                 hub.api.http

- ``hub.ris.mapping``: je Quelle eine Abbildung auf das kanonische Modell. Das Ergebnis ist in beiden
  Fällen ein OParl-Objekt (``hub.ris.canonical.Objekt``) aus denselben Bausteinen.
- ``hub.api.serialization``: die eine Serialisierung – aus kanonischen Objekten werden Antworten:
  Zeitfilter, Blättern, Listen-Hülle mit ``Link``-Header, gekürzte Objekte für Gelöschtes in
  inkrementellen Listen.
- ``hub.api.http``: die HTTP-Hülle aller Endpunkte – JSON mit ``ETag``, bedingte Anfragen (``304``),
  Fehler als JSON, CORS, Ratenbegrenzung, nur lesende Methoden.

Eine Ausgabe legt nur noch fest, welche Objekte sichtbar sind und unter welchen Adressen sie stehen.
Adressen und Kennungen bestehender Objekte ändern sich nicht; abweichende Schreibweisen einer Adresse
leiten auf die gültige weiter (``hub.api.urls``).
"""
