# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abruf der Bewertungen von KI-Zusammenfassungen durch das ERP (Issue #1221).

``GET /api/insight/ki-bewertungen/?seit=JJJJ-MM-TT&nach=<id>&anzahl=<n>``: Einzelwerte ohne Personenbezug
(``services.ki_bewertung.fuer_erp``). Zugang wie die Provisioning-API v2, die das ERP schon nutzt: Schlüssel
``PROVISIONING_V2_API_KEY`` als ``Authorization: Bearer``, Netze ``PROVISIONING_V2_ALLOWED_NETWORKS``; ohne
Schlüssel 404, falscher Schlüssel 401.
"""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest, JsonResponse

from apps.provisioning.v2.views import ProvisioningV2View

from ..services import ki_bewertung


class KiBewertungenView(ProvisioningV2View):
    """GET: Bewertungen ab ``seit`` (Tag der Abgabe oder Änderung), seitenweise nach Kennung."""

    def get(self, request: HttpRequest) -> JsonResponse:
        try:
            seit = date.fromisoformat(request.GET["seit"]) if request.GET.get("seit") else None
            nach = int(request.GET.get("nach") or 0)
            anzahl = int(request.GET.get("anzahl") or ki_bewertung.ERP_SEITE_MAX)
        except ValueError:
            return JsonResponse({"error": "validation", "fields": {"seit": "JJJJ-MM-TT", "nach": "Zahl"}}, status=400)
        return JsonResponse(ki_bewertung.fuer_erp(seit=seit, nach=nach, anzahl=anzahl))
