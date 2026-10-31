# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API v2: HTTP-Schicht (Issue #1132).

Zugang nur mit eigenem Schlüssel ``PROVISIONING_V2_API_KEY`` (Header ``Authorization: Bearer <key>``).
Ohne Schlüssel ist v2 abgeschaltet und antwortet überall mit 404; ebenso für Absender außerhalb von
``PROVISIONING_V2_ALLOWED_NETWORKS``, wenn gesetzt. Falscher oder fehlender Schlüssel: 401.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.http import Http404, HttpRequest, JsonResponse
from django.http.response import HttpResponseBase
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.two_factor_policy import IPNetwork, client_ip, ip_in_networks

from . import selectors, services


def _configured_key() -> str:
    return str(getattr(settings, "PROVISIONING_V2_API_KEY", "") or "")


@lru_cache(maxsize=8)
def _parse_networks(raw: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in raw)


def _network_allowed(request: HttpRequest) -> bool:
    """Absender in ``PROVISIONING_V2_ALLOWED_NETWORKS`` (leer = keine Beschränkung)."""
    raw = tuple(getattr(settings, "PROVISIONING_V2_ALLOWED_NETWORKS", ()) or ())
    if not raw:
        return True
    return ip_in_networks(client_ip(request), _parse_networks(raw))


def _token_valid(request: HttpRequest, key: str) -> bool:
    """Konstantzeit-Vergleich des Bearer-Tokens (als Bytes, auch bei Nicht-ASCII-Zeichen im Header)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].encode("utf-8"), key.encode("utf-8"))


class ProvisioningV2View(View):
    """Basisklasse: abgeschaltet → 404, fremdes Netz → 404, ohne gültigen Schlüssel → 401."""

    @method_decorator(csrf_exempt)
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        key = _configured_key()
        if not key or not _network_allowed(request):
            # Nach außen unsichtbar: keine Auskunft, ob es die Schnittstelle gibt
            raise Http404
        if not _token_valid(request, key):
            response = JsonResponse({"error": "unauthorized"}, status=401)
            response["WWW-Authenticate"] = "Bearer"
            return response
        return super().dispatch(request, *args, **kwargs)


class WorkCollectionView(ProvisioningV2View):
    """GET /api/provisioning/v2/work/ — alle Organisationen, auch ohne Referenz."""

    def get(self, request: HttpRequest) -> JsonResponse:
        states = selectors.list_work_states()
        return JsonResponse({"organizations": states, "count": len(states)})


class WorkDetailView(ProvisioningV2View):
    """GET/PUT /api/provisioning/v2/work/<ref> — Ist-Zustand lesen bzw. Soll-Zustand setzen."""

    def get(self, request: HttpRequest, ref: str) -> JsonResponse:
        organization = selectors.find_by_ref(ref) if services.is_valid_ref(ref) else None
        if organization is None:
            return JsonResponse({"error": "not_found"}, status=404)
        return JsonResponse(selectors.work_state(organization))

    def put(self, request: HttpRequest, ref: str) -> JsonResponse:
        if not services.is_valid_ref(ref):
            return JsonResponse(
                {
                    "error": "validation",
                    "fields": {"ref": "1-64 Zeichen aus A-Z, a-z, 0-9, Punkt, Unterstrich, Bindestrich"},
                },
                status=400,
            )
        try:
            data = json.loads(request.body.decode("utf-8"))
        except ValueError:  # auch JSONDecodeError und UnicodeDecodeError
            return JsonResponse({"error": "invalid_json"}, status=400)
        try:
            result = services.put_work_organization(ref, services.parse_work_spec(data))
        except services.ProvisioningError as exc:
            return JsonResponse(exc.payload(), status=exc.status)

        payload = selectors.work_state(result.organization)
        payload["changed"] = bool(result.changed)
        if result.created:
            payload["invitation_sent"] = result.invitation_sent
        if result.adopted:
            payload["adopted"] = True
        return JsonResponse(payload, status=201 if result.created else 200)
