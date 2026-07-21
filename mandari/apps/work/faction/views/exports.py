# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PDF-Exporte für Fraktionssitzungen (Issue #60).

Niederschrift als PDF in zwei Fassungen:
- oeffentlich: nur Ö-TOPs und deren Einträge (protocols.view_public)
- intern: inkl. NÖ-Teil und TOP-loser Einträge — nur für Vereidigte mit
  Zugriff auf den nicht-öffentlichen Teil und protocols.view_full
"""

import logging

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from ..models import FactionMeeting

logger = logging.getLogger(__name__)


class FactionProtocolPdfView(WorkViewMixin, View):
    """Niederschrift-PDF herunterladen (öffentliche oder interne Fassung)."""

    permission_required = "faction.view_public"

    def get(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        variant = kwargs.get("variant", "oeffentlich")
        if variant not in ("oeffentlich", "intern"):
            return HttpResponse(status=404)
        internal = variant == "intern"

        if internal:
            # Zentrale Sichtbarkeitsfunktion (Issue #64): NÖ nur für Vereidigte
            from ..visibility import can_view_internal

            if not (can_view_internal(self.membership) and self.membership.has_permission("protocols.view_full")):
                return HttpResponse(status=403)
        else:
            if not self.membership.has_permission("protocols.view_public"):
                return HttpResponse(status=403)

        from ..services import build_faction_protocol_pdf

        pdf_bytes = build_faction_protocol_pdf(meeting, internal=internal)

        filename = f"niederschrift-{meeting.start.strftime('%Y-%m-%d')}-{variant}.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
