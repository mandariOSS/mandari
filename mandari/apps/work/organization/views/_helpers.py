# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Request-Helfer der Organisations-Views (Issue #160).

Fachlogik und Datenzugriff liegen in ``apps.work.organization.selectors`` /
``apps.work.organization.services``; hier bleibt die Übersetzung von
``ServiceError`` in Django-Messages.
"""

from django.contrib import messages
from django.http import HttpRequest

from ..services import ServiceError


def flash_error(request: HttpRequest, exc: ServiceError) -> None:
    """Fachlichen Fehler mit seiner Meldungsstufe (error/warning/info) anzeigen."""
    messages.add_message(request, exc.level, str(exc))
