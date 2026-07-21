# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Persönlicher iCal-Feed-Endpoint (Issue #70).

Abruf OHNE Login — Kalender-Clients können sich nicht anmelden. Die
Sicherheit liegt ausschließlich im opaken Zufalls-Token; unbekannte
Tokens werden mit 404 abgewiesen (nicht enumerierbar). Nach einer
Token-Erneuerung in den Profileinstellungen ist die alte URL sofort
ungültig.
"""

import logging

from django.http import Http404, HttpResponse
from django.views.generic import View

from ..models import CalendarFeedToken

logger = logging.getLogger(__name__)


class PersonalCalendarFeedView(View):
    """iCal-Feed einer Benutzer:in ausliefern (Token-geschützt, ohne Login)."""

    def get(self, request, *args, **kwargs):
        token = kwargs.get("token", "")
        feed_token = CalendarFeedToken.objects.select_related("user").filter(token=token, user__is_active=True).first()
        if feed_token is None:
            raise Http404("Unbekanntes Feed-Token.")

        from ..feeds import build_personal_feed

        ics_bytes = build_personal_feed(feed_token.user)
        response = HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = 'inline; filename="mandari-sitzungen.ics"'
        # Kalender-Clients pollen regelmäßig — kurzes privates Caching genügt
        response["Cache-Control"] = "private, max-age=300"
        response["X-Content-Type-Options"] = "nosniff"
        return response
