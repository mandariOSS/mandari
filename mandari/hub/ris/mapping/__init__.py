# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildungen der Quellen auf das kanonische RIS-Modell.

Je Quelle genau ein Modul – die einzige Stelle, die deren Objekte in das Modell übersetzt:

- ``session``: Objekte von mandari Session (Mandant, Gremien, Personen, Sitzungen, Vorlagen, Anlagen)
- ``bestand``: Objekte des RIS-Bestands (aus fremden RIS geerntet oder von Session-Mandanten gespiegelt)

Beide Abbildungen bieten je Objekttyp eine Methode mit demselben Namen (``organization``, ``meeting``,
``paper`` …, ``tombstone`` für Gelöschtes) und nennen Adressen über ``uris``; das Ergebnis ist ein
OParl-Objekt aus den Bausteinen in ``hub.ris.canonical``. Ausgegeben werden sie von ``hub.api``.

Eine Abbildung ist versioniert (``VERSION``): Ändert sich, was für dasselbe Quellobjekt herauskommt,
steigt die Version, damit abgeleitete Sichten neu aufgebaut werden können.
"""
