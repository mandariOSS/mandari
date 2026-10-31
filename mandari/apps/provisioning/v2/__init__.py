# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API v2: deklarative Bereitstellung von Work-Organisationen (Issue #1132).

Ein aufrufendes System (etwa ein ERP mit Soll-Ist-Abgleich) setzt per ``PUT`` den Soll-Zustand einer
Organisation unter seiner eigenen Kennung (``ref``) und liest per ``GET`` den Ist-Zustand. Wiederholte
Aufrufe mit demselben Inhalt ändern nichts und verschicken keine weitere Einladung. v1
(``apps.provisioning.views``) bleibt unverändert daneben bestehen. Beschreibung: docs/PROVISIONING_API.md.
"""
