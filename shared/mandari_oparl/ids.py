# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanonische Kennungen für Objekte des RIS-Bestands.

Architekturentscheidung: ``docs/adr/20260929-kanonisches-modell.md``.

Jedes RIS-Objekt trägt die Kennung ``uuid5(NS_MANDARI_RIS, kanonische URI)``:

- **Fremd-RIS:** Die URI ist die ``id`` des Objekts in der Quelle, genau so, wie die Quelle sie liefert.
  Die Funktion normalisiert nichts; jede Umformung würde bestehende Kennungen verändern.
- **Eigene Session-Objekte:** Die URI ist die öffentliche OParl-URL auf Basis der konfigurierten Adresse
  der Installation (``SITE_URL``), nicht des Hosts der Anfrage.

Ingestor und Django verwenden ausschließlich diese Funktion. Gleiche URIs ergeben überall dieselbe
Kennung, auch über Installationen hinweg. Beide Testsuiten prüfen dieselben Testvektoren
(``ids_testvektoren.json`` in diesem Paket).

Namensraum: ``NS_MANDARI_RIS`` ist der URL-Namensraum aus RFC 9562 (``uuid.NAMESPACE_URL``), denn die
kanonische URI ist eine URL. Der Ingestor vergibt seine Kennungen von Beginn an so; die Kennungen des
Bestands aus Fremd-RIS sind damit bereits kanonisch. Der Namensraum darf sich nie ändern: Jede Änderung
würde alle Kennungen, Links und Suchindex-Einträge ungültig machen.
"""

from uuid import NAMESPACE_URL, UUID, uuid5

#: Namensraum der kanonischen RIS-Kennungen (RFC 9562, URL-Namensraum). Unveränderlich.
NS_MANDARI_RIS: UUID = NAMESPACE_URL


def canonical_id(uri: str) -> UUID:
    """
    Kanonische Kennung eines RIS-Objekts: ``uuid5(NS_MANDARI_RIS, uri)``.

    Die URI geht unverändert ein (keine Normalisierung von Schema, Host, Groß-/Kleinschreibung oder
    abschließendem Schrägstrich). Leere URIs ergeben keine sinnvolle Kennung; Aufrufer prüfen das vorher.
    """
    return uuid5(NS_MANDARI_RIS, uri)
