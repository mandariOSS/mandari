# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildungen der Quellen auf das kanonische RIS-Modell.

Je Quelle genau ein Modul – die einzige Stelle, die deren Objekte in das Modell übersetzt:

- ``session``: Objekte von mandari Session (Mandant, Gremien, Personen, Sitzungen, Vorlagen, Anlagen)

Eine Abbildung ist versioniert (``VERSION``): Ändert sich, was für dasselbe Quellobjekt herauskommt,
steigt die Version, damit abgeleitete Sichten neu aufgebaut werden können.
"""
