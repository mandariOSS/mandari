# SPDX-License-Identifier: AGPL-3.0-or-later
"""Antwort für Objekte, die aus der Öffentlichkeit zurückgenommen sind (``SourceDeletionModel.withdrawn_by_publisher``)."""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string

from ..models import SESSION_OPARL_MARKERS


def withdrawn_response(request: HttpRequest, obj: Any) -> HttpResponse:
    """410 Gone ohne jeden Inhalt des Objekts – nur ein neutraler Hinweis."""
    # Nur bei Objekten aus mandari Session hat die Verwaltung selbst zurückgenommen; für Objekte fremder
    # Systeme nennt der Hinweis weder einen Urheber noch einen Grund
    ext = getattr(obj, "external_id", "") or ""
    context = {
        "withdrawn_at": obj.deleted_at,
        "von_verwaltung": all(marker in ext for marker in SESSION_OPARL_MARKERS),
    }
    html = render_to_string("pages/withdrawn.html", context, request=request)
    response = HttpResponse(html, status=410)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    return response
