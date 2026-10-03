# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne des Bürgerportals (``apps.events.schedule``, Issue #515).

- ``verortung_automatisch``: alle ``GEOREF_AUTO_INTERVAL_MINUTES`` (Standard 15) Minuten ein begrenzter
  Verortungslauf (Regex/Gazetteer, ``GEOREF_AUTO_LIMIT`` Vorlagen, abschaltbar mit
  ``GEOREF_AUTO_ENABLED=false``). Bis Issue #515 lief er in einem Faden im Webprozess.
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import every

from .services.georef_runner import run_auto_georef_pass


@every(minutes=max(1, int(settings.GEOREF_AUTO_INTERVAL_MINUTES)))
@task
def verortung_automatisch() -> None:
    """Begrenzter automatischer Verortungslauf; eine Cache-Sperre verhindert parallele Läufe."""
    run_auto_georef_pass()
