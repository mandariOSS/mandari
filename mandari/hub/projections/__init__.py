# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichten der Datendrehscheibe, die aus Ereignissen entstehen (``docs/adr/20260929-ereignistechnik-postgres.md``).

- ``hub.projections.ris_session``: RIS-Projektor für Session-Mandanten (Issue #536). Schreibt aus den Ereignissen
  von mandari Session, was der Spiegel heute in den RIS-Bestand übernimmt – vorerst nur in die Schatten-Quelle
  (``models.RisSchatten``), nie in den Bestand.
- ``hub.projections.ris_vergleich``: Vergleich der Schatten-Quelle mit dem RIS-Bestand.

Die Drehscheibe importiert kein Fachmodul: Was der Projektor von Session braucht (Objekte in der Abbildung der
Schnittstelle, Kennungen), reicht Session als ``ris_session.Quelle`` herein (``apps.session.ris_projektion``).
"""
