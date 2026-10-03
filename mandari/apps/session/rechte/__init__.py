# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechte mit Geltungsbereich in Session (Issue #759, ADR ``docs/adr/20261003-rechte-mit-geltungsbereich.md``).

Ein Recht ist Objektart + Aktion (``vorlage.freigeben``) und wird gegen einen Geltungsbereich ausgewertet. Rollen
bündeln Rechte, eine Zuweisung gibt eine Rolle einem Konto für einen Geltungsbereich und einen Zeitraum.

- :mod:`.katalog` – Rechtekatalog mit der Herkunft jedes Rechts aus den bisherigen Häkchen der Rolle
- :mod:`.kern` – fachfreier Kern: Bereiche, Bäume, Zuweisungen auswerten, Zugriffskontext
- :mod:`.bereiche` – die Bäume eines Mandanten (Körperschaft → Gremien, Verwaltungsaufbau der Ämter)
- :mod:`.aufloesung` – Zugriffskontext eines Kontos und die bisherigen Rechtenamen daraus
- :mod:`.zuweisungen` – Zuweisen, Aufheben, Spiegel von ``SessionUser.roles`` und Abgleich nach ``migrate``

Übergang (Teil 1, #772): ``SessionUser.roles`` bleibt die maßgebliche Quelle für mandantenweite, unbefristete Rollen.
Befristete Zuweisungen und Zuweisungen mit Geltungsbereich wirken nur bei eingeschaltetem Schalter des Mandanten.
"""
