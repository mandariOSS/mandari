# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Regeln aller Schnittstellen als Code (``docs/adr/20261105-eine-schnittstelle-api-first.md``).

Fachfreie Bausteine der Plattform; jede Schnittstelle nutzt sie, statt die Regel selbst nachzubauen:

- ``problems``: Fehler als ``application/problem+json`` nach RFC 9457 (``Problem``, ``ProblemError``,
  ``problem_response``), dazu die Handler für django-ninja (``install_problem_handlers``).
- ``conditional``: ``ETag`` über den Inhalt und bedingte Abrufe mit ``If-None-Match`` (``304``).
- ``deprecation``: Abkündigung mit den Headern ``Deprecation`` (RFC 9745) und ``Sunset`` (RFC 8594).
- ``idempotency``: der Header ``Idempotency-Key`` schreibender Befehle (IETF-Entwurf, A6).
- ``pages``: Listen in Seiten wie in OParl (``data``, ``pagination``, ``links`` und ``Link``-Header).

Was eine Schnittstelle fachlich ausgibt, steht beim Eigentümer; Einstieg und Gesamtbeschreibung liegen in
der Drehscheibe (``hub.api``).
"""
