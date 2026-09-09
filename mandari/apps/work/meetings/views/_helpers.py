# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Request-Helfer der Vorbereitungs-Views (Issue #160).

Fachlogik und Datenzugriff liegen in ``apps.work.meetings.selectors`` /
``apps.work.meetings.services``; hier bleibt nur das Parsen der Anfrage und die
einheitliche JSON-Fehlerantwort.
"""

import json
from collections.abc import Mapping
from typing import Any

from django.http import HttpRequest, JsonResponse


def request_payload(request: HttpRequest) -> Mapping[str, Any]:
    """JSON-Body oder Formulardaten als Mapping (beide Varianten werden von der UI genutzt)."""
    if request.content_type == "application/json":
        data: dict[str, Any] = json.loads(request.body)
        return data
    return request.POST


def unauthorized() -> JsonResponse:
    """Einheitliche 403-Antwort ohne Mitgliedschaft bzw. Org-Grenze."""
    return JsonResponse({"error": "Unauthorized"}, status=403)


def error_response(message: str, status: int = 400) -> JsonResponse:
    """Fachlicher Fehler als JSON."""
    return JsonResponse({"error": message}, status=status)
