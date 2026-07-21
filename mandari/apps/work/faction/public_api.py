# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche API v1 für Fraktionssitzungen (Issue #71).

Read-only-JSON-API für die Einbindung öffentlicher Sitzungstermine und
Tagesordnungen auf der Fraktions-Webseite:

- Stabile, versionierte Pfade unter ``/api/public/v1/...`` (pfadbasiert —
  ein späteres Routing über die Subdomain api.mandari.de ist ein reines
  Caddy-Thema und ändert die Pfade nicht).
- Aktivierung je Organisation als bewusstes Opt-in (Default: AUS,
  Fraktionseinstellungen).
- Zugriff über ein opakes Zufalls-Token in der URL (kein Org-Slug, nicht
  enumerierbar; Token regenerierbar). Unbekannte, deaktivierte oder
  inaktive Zugänge liefern einheitlich 404.
- CORS (``Access-Control-Allow-Origin: *``) für die Browser-Einbindung,
  Cache-Header für sinnvolles Caching.

Inhalte — STRIKT öffentlich:
- Sitzungen (kommende + vergangene im konfigurierten Zeitraum) mit
  Datum/Ort/Status; Entwürfe erscheinen nie.
- Je Sitzung ausschließlich ÖFFENTLICHE, angenommene TOPs (Nummer,
  Titel). NÖ-TOPs erscheinen niemals — auch nicht als Platzhalter.
- Niemals Protokollinhalte, Beschlüsse, Teilnehmer, Video-Links oder
  interne Beschreibungen.

Dokumentation: docs/FACTION_PUBLIC_API.md + OpenAPI-Schema-Endpoint.
"""

from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.utils import timezone
from django.views.generic import View

from .models import FactionMeeting, FactionPublicApiAccess

API_VERSION = "1.0"

# Statuswerte, die öffentlich sichtbar sind (niemals Entwürfe)
PUBLIC_STATUSES = ("planned", "invited", "ongoing", "completed", "cancelled")


# =============================================================================
# Antwort-Helfer (JSON + CORS + Caching)
# =============================================================================


def _apply_cors(response):
    """CORS-Header für die Browser-Einbindung auf Fraktions-Webseiten."""
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Content-Type"
    response["Access-Control-Max-Age"] = "86400"
    return response


def _json_response(data: dict, status: int = 200, cache_seconds: int = 300) -> JsonResponse:
    response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    if status == 200 and cache_seconds:
        response["Cache-Control"] = f"public, max-age={cache_seconds}"
    else:
        response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return _apply_cors(response)


def _not_found() -> JsonResponse:
    """Einheitliche 404-Antwort — verrät nicht, ob ein Token existiert."""
    return _json_response(
        {"error": "not_found", "detail": "Unbekannter oder deaktivierter API-Zugang."},
        status=404,
        cache_seconds=0,
    )


def _resolve_access(token: str):
    """Aktivierten API-Zugang zum Token auflösen (None bei unbekannt/deaktiviert)."""
    if not token:
        return None
    return (
        FactionPublicApiAccess.objects.select_related("organization")
        .filter(token=token, is_enabled=True, organization__is_active=True)
        .first()
    )


# =============================================================================
# Serialisierung — ausschließlich öffentliche Felder
# =============================================================================


def _serialize_meeting(meeting, *, include_agenda: bool = False) -> dict:
    """
    Sitzung als öffentliches JSON.

    Bewusst NICHT enthalten: Beschreibung, Video-Link, Protokoll,
    Beschlüsse, Teilnehmer — und niemals NÖ-TOPs.
    """
    data = {
        "id": str(meeting.id),
        "title": meeting.title,
        "number": meeting.meeting_number or None,
        "start": meeting.start.isoformat() if meeting.start else None,
        "end": meeting.end.isoformat() if meeting.end else None,
        "location": meeting.location or ("Online" if meeting.is_virtual else ""),
        "is_virtual": meeting.is_virtual,
        "status": meeting.status,
        "cancelled": meeting.status == "cancelled",
    }
    if include_agenda:
        items = meeting.agenda_items.filter(
            visibility="public", proposal_status="active", parent__isnull=True
        ).order_by("order", "number")
        agenda = []
        for item in items:
            agenda.append({"number": item.number, "title": item.title})
            for child in item.children.filter(visibility="public", proposal_status="active").order_by(
                "order", "number"
            ):
                agenda.append({"number": child.number, "title": child.title})
        data["agenda"] = agenda
    return data


def _meeting_window(access):
    """Zeitfenster: konfigurierte Vergangenheit bis ~13 Monate Zukunft."""
    now = timezone.now()
    return now - timedelta(days=access.past_days), now + timedelta(days=400)


# =============================================================================
# Endpoints
# =============================================================================


class PublicApiBaseView(View):
    """Basis: OPTIONS-Preflight für CORS."""

    def options(self, request, *args, **kwargs):
        return _apply_cors(HttpResponse(status=204))


class PublicApiRootView(PublicApiBaseView):
    """Informationen zum API-Zugang einer Organisation."""

    def get(self, request, *args, **kwargs):
        access = _resolve_access(kwargs.get("token", ""))
        if access is None:
            return _not_found()
        base = f"/api/public/v1/fraktionen/{access.token}"
        return _json_response(
            {
                "api_version": API_VERSION,
                "organization": {"name": access.organization.name},
                "endpoints": {
                    "meetings": f"{base}/sitzungen/",
                    "meeting_detail": f"{base}/sitzungen/{{id}}/",
                    "openapi": "/api/public/v1/openapi.json",
                },
            }
        )


class PublicMeetingListView(PublicApiBaseView):
    """Öffentliche Terminliste: kommende + vergangene Sitzungen (Zeitraum konfigurierbar)."""

    def get(self, request, *args, **kwargs):
        access = _resolve_access(kwargs.get("token", ""))
        if access is None:
            return _not_found()

        window_start, window_end = _meeting_window(access)
        meetings = FactionMeeting.objects.filter(
            organization=access.organization,
            status__in=PUBLIC_STATUSES,
            start__gte=window_start,
            start__lte=window_end,
        ).order_by("start")
        return _json_response(
            {
                "api_version": API_VERSION,
                "organization": {"name": access.organization.name},
                "count": meetings.count(),
                "meetings": [_serialize_meeting(m) for m in meetings],
            }
        )


class PublicMeetingDetailView(PublicApiBaseView):
    """Sitzungsdetail mit öffentlicher Tagesordnung (nur Ö-TOPs: Nummer, Titel)."""

    def get(self, request, *args, **kwargs):
        access = _resolve_access(kwargs.get("token", ""))
        if access is None:
            return _not_found()

        meeting = FactionMeeting.objects.filter(
            id=kwargs.get("meeting_id"),
            organization=access.organization,
            status__in=PUBLIC_STATUSES,
        ).first()
        if meeting is None:
            return _json_response({"error": "not_found", "detail": "Unbekannte Sitzung."}, status=404, cache_seconds=0)

        data = _serialize_meeting(meeting, include_agenda=True)
        data["api_version"] = API_VERSION
        data["organization"] = {"name": access.organization.name}
        return _json_response(data)


class OpenApiSchemaView(PublicApiBaseView):
    """OpenAPI-3-Schema der öffentlichen API v1 (ohne Token abrufbar)."""

    def get(self, request, *args, **kwargs):
        return _json_response(build_openapi_schema(), cache_seconds=3600)


def build_openapi_schema() -> dict:
    """OpenAPI-3.0-Schema der öffentlichen Fraktions-API v1."""
    base_url = getattr(settings, "SITE_URL", "").rstrip("/")
    meeting_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "title": {"type": "string"},
            "number": {"type": "integer", "nullable": True},
            "start": {"type": "string", "format": "date-time", "nullable": True},
            "end": {"type": "string", "format": "date-time", "nullable": True},
            "location": {"type": "string"},
            "is_virtual": {"type": "boolean"},
            "status": {
                "type": "string",
                "enum": list(PUBLIC_STATUSES),
            },
            "cancelled": {"type": "boolean"},
        },
    }
    agenda_item_schema = {
        "type": "object",
        "description": "Öffentlicher Tagesordnungspunkt (nicht-öffentliche TOPs erscheinen niemals).",
        "properties": {
            "number": {"type": "string"},
            "title": {"type": "string"},
        },
    }
    token_param = {
        "name": "token",
        "in": "path",
        "required": True,
        "description": "Opakes API-Token der Organisation (Fraktionseinstellungen).",
        "schema": {"type": "string"},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Mandari — Öffentliche Fraktions-API",
            "version": API_VERSION,
            "description": (
                "Read-only-API für öffentliche Termine und Tagesordnungen von "
                "Fraktionssitzungen. Aktivierung je Organisation (Opt-in); Zugriff "
                "über ein opakes Token in der URL. Ausschließlich öffentliche "
                "Inhalte — keine nicht-öffentlichen Tagesordnungspunkte, keine "
                "Protokolle, keine Beschlüsse, keine Teilnehmerdaten."
            ),
        },
        "servers": [{"url": f"{base_url}/api/public/v1"}],
        "paths": {
            "/fraktionen/{token}/": {
                "get": {
                    "summary": "API-Zugang und Endpunkte",
                    "parameters": [token_param],
                    "responses": {
                        "200": {"description": "Zugang aktiv"},
                        "404": {"description": "Unbekannter oder deaktivierter Zugang"},
                    },
                }
            },
            "/fraktionen/{token}/sitzungen/": {
                "get": {
                    "summary": "Öffentliche Sitzungstermine (kommend + vergangen)",
                    "parameters": [token_param],
                    "responses": {
                        "200": {
                            "description": "Terminliste",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "api_version": {"type": "string"},
                                            "organization": {
                                                "type": "object",
                                                "properties": {"name": {"type": "string"}},
                                            },
                                            "count": {"type": "integer"},
                                            "meetings": {
                                                "type": "array",
                                                "items": {"$ref": "#/components/schemas/Meeting"},
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "404": {"description": "Unbekannter oder deaktivierter Zugang"},
                    },
                }
            },
            "/fraktionen/{token}/sitzungen/{id}/": {
                "get": {
                    "summary": "Sitzungsdetail mit öffentlicher Tagesordnung",
                    "parameters": [
                        token_param,
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Sitzung mit öffentlichen TOPs",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [
                                            {"$ref": "#/components/schemas/Meeting"},
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "agenda": {
                                                        "type": "array",
                                                        "items": {"$ref": "#/components/schemas/AgendaItem"},
                                                    }
                                                },
                                            },
                                        ]
                                    }
                                }
                            },
                        },
                        "404": {"description": "Unbekannte Sitzung oder unbekannter Zugang"},
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "Dieses OpenAPI-Schema",
                    "responses": {"200": {"description": "OpenAPI-3.0-Schema"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Meeting": meeting_schema,
                "AgendaItem": agenda_item_schema,
            }
        },
    }


# =============================================================================
# URL-Patterns (eingebunden unter /api/public/v1/ in mandari/urls.py)
# =============================================================================

app_name = "faction_public_api"

urlpatterns = [
    path("openapi.json", OpenApiSchemaView.as_view(), name="openapi"),
    path("fraktionen/<slug:token>/", PublicApiRootView.as_view(), name="root"),
    path("fraktionen/<slug:token>/sitzungen/", PublicMeetingListView.as_view(), name="meetings"),
    path(
        "fraktionen/<slug:token>/sitzungen/<uuid:meeting_id>/",
        PublicMeetingDetailView.as_view(),
        name="meeting_detail",
    ),
]
