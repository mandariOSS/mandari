# SPDX-License-Identifier: AGPL-3.0-or-later
"""Antwort für Objekte, die der Herausgeber (mandari Session) aus der Öffentlichkeit zurückgenommen hat."""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string


def withdrawn_response(request: HttpRequest, obj: Any) -> HttpResponse:
    """410 Gone ohne jeden Inhalt des Objekts – nur ein neutraler Hinweis."""
    html = render_to_string("pages/withdrawn.html", {"withdrawn_at": obj.deleted_at}, request=request)
    response = HttpResponse(html, status=410)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    return response
