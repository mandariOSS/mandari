# SPDX-License-Identifier: AGPL-3.0-or-later
"""
End-to-End-Tests mit Playwright (Chromium) gegen den Django-Live-Server.

Ausführen (aus ``mandari/``, gebaute Vite-Assets vorausgesetzt: ``npm run build``)::

    MANDARI_E2E=1 pytest tests_e2e -q

Die Tests prüfen Kernpfade im Browser (JavaScript aktiv), laufen axe-core (Barrierefreiheit)
und legen Screenshots unter ``tests_e2e/screenshots/`` ab (CI-Artefakt). Ohne ``MANDARI_E2E=1``
werden sie übersprungen, damit ``pytest`` ohne Browser funktioniert.
"""
