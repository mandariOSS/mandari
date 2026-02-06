# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session API views.

Provides:
- OParl 1.1 compliant API for public data
- Extended Session API for non-public data (authenticated)
"""

import json
from datetime import datetime
from typing import Any

from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.session.models import (
    SessionAPIToken,
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionTenant,
)
from apps.session.permissions import SessionPermissionChecker


def get_client_ip(request) -> str:
    """Get the client's IP address from request."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def build_url(request, name: str, **kwargs) -> str:
    """Build absolute URL for API responses."""
    return request.build_absolute_uri(reverse(name, kwargs=kwargs))


def oparl_datetime(dt: datetime | None) -> str | None:
    """Format datetime for OParl (ISO 8601)."""
    if dt is None:
        return None
    return dt.isoformat()


def oparl_date(d) -> str | None:
    """Format date for OParl (ISO 8601)."""
    if d is None:
        return None
    return d.isoformat()


class OParlMixin:
    """Mixin for OParl API views."""

    def get_tenant(self, tenant_slug: str) -> SessionTenant:
        """Get tenant by slug."""
        try:
            return SessionTenant.objects.get(slug=tenant_slug, is_active=True)
        except SessionTenant.DoesNotExist:
            raise Http404("Mandant nicht gefunden")

    def json_response(self, data: Any, status: int = 200) -> JsonResponse:
        """Return JSON response with proper headers."""
        response = JsonResponse(data, safe=False, json_dumps_params={"ensure_ascii": False})
        response["Content-Type"] = "application/json; charset=utf-8"
        # CORS headers only for OParl (public) endpoints
        # Session API endpoints should not have wildcard CORS
        return response

    def oparl_json_response(self, data: Any, status: int = 200) -> JsonResponse:
        """Return JSON response with CORS headers for OParl (public) API."""
        response = JsonResponse(data, safe=False, json_dumps_params={"ensure_ascii": False})
        response["Content-Type"] = "application/json; charset=utf-8"
        # OParl is public API - allow CORS for data consumers
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type"
        return response


# =============================================================================
# API ROOT
# =============================================================================


class APIRootView(OParlMixin, View):
    """API root endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        return self.json_response(
            {
                "name": f"Session API - {tenant.name}",
                "version": "1.0",
                "oparl": build_url(request, "session:oparl_system", tenant_slug=tenant_slug),
                "session": {
                    "meetings": build_url(request, "session:api_meetings", tenant_slug=tenant_slug),
                    "papers": build_url(request, "session:api_papers", tenant_slug=tenant_slug),
                    "applications": build_url(request, "session:api_applications", tenant_slug=tenant_slug),
                },
            }
        )


# =============================================================================
# OPARL API (PUBLIC)
# =============================================================================


class OParlSystemView(OParlMixin, View):
    """OParl System endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        return self.oparl_json_response(
            {
                "id": build_url(request, "session:oparl_system", tenant_slug=tenant_slug),
                "type": "https://schema.oparl.org/1.1/System",
                "oparlVersion": "https://schema.oparl.org/1.1/",
                "name": f"Session RIS - {tenant.name}",
                "contactEmail": tenant.contact_email or None,
                "contactName": tenant.name,
                "website": tenant.website or None,
                "vendor": "https://mandari.org",
                "product": "https://mandari.org/session",
                "body": build_url(request, "session:oparl_bodies", tenant_slug=tenant_slug),
                "otherOparlVersions": [],
                "created": oparl_datetime(tenant.created_at),
                "modified": oparl_datetime(tenant.updated_at),
            }
        )


class OParlBodyListView(OParlMixin, View):
    """OParl Body list endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # For Session, there's one body per tenant
        body = self._build_body(request, tenant)

        return self.oparl_json_response(
            {
                "data": [body],
                "pagination": {
                    "totalElements": 1,
                    "elementsPerPage": 100,
                    "currentPage": 1,
                    "totalPages": 1,
                },
                "links": {},
            }
        )

    def _build_body(self, request, tenant: SessionTenant) -> dict:
        """Build OParl Body object."""
        return {
            "id": build_url(request, "session:oparl_body", tenant_slug=tenant.slug),
            "type": "https://schema.oparl.org/1.1/Body",
            "name": tenant.name,
            "shortName": tenant.short_name or None,
            "website": tenant.website or None,
            "contactEmail": tenant.contact_email or None,
            "classification": "Kommune",
            "organization": build_url(request, "session:oparl_organizations", tenant_slug=tenant.slug),
            "person": build_url(request, "session:oparl_persons", tenant_slug=tenant.slug),
            "meeting": build_url(request, "session:oparl_meetings", tenant_slug=tenant.slug),
            "paper": build_url(request, "session:oparl_papers", tenant_slug=tenant.slug),
            "created": oparl_datetime(tenant.created_at),
            "modified": oparl_datetime(tenant.updated_at),
        }


class OParlBodyView(OParlMixin, View):
    """OParl single Body endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        return self.oparl_json_response(
            {
                "id": build_url(request, "session:oparl_body", tenant_slug=tenant.slug),
                "type": "https://schema.oparl.org/1.1/Body",
                "name": tenant.name,
                "shortName": tenant.short_name or None,
                "website": tenant.website or None,
                "contactEmail": tenant.contact_email or None,
                "classification": "Kommune",
                "organization": build_url(request, "session:oparl_organizations", tenant_slug=tenant.slug),
                "person": build_url(request, "session:oparl_persons", tenant_slug=tenant.slug),
                "meeting": build_url(request, "session:oparl_meetings", tenant_slug=tenant.slug),
                "paper": build_url(request, "session:oparl_papers", tenant_slug=tenant.slug),
                "created": oparl_datetime(tenant.created_at),
                "modified": oparl_datetime(tenant.updated_at),
            }
        )


class OParlOrganizationListView(OParlMixin, View):
    """OParl Organization list endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # Get public organizations
        orgs = SessionOrganization.objects.filter(
            tenant=tenant,
            is_active=True,
        ).order_by("name")

        data = []
        for org in orgs:
            data.append(
                {
                    "id": build_url(
                        request,
                        "session:organization_detail",
                        tenant_slug=tenant.slug,
                        organization_id=org.id,
                    ),
                    "type": "https://schema.oparl.org/1.1/Organization",
                    "body": build_url(request, "session:oparl_body", tenant_slug=tenant.slug),
                    "name": org.name,
                    "shortName": org.short_name or None,
                    "organizationType": org.organization_type,
                    "classification": org.get_organization_type_display(),
                    "startDate": oparl_date(org.start_date),
                    "endDate": oparl_date(org.end_date),
                    "created": oparl_datetime(org.created_at),
                    "modified": oparl_datetime(org.updated_at),
                }
            )

        return self.oparl_json_response(
            {
                "data": data,
                "pagination": {
                    "totalElements": len(data),
                    "elementsPerPage": 100,
                    "currentPage": 1,
                    "totalPages": 1,
                },
                "links": {},
            }
        )


class OParlPersonListView(OParlMixin, View):
    """OParl Person list endpoint."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # Get active persons
        persons = SessionPerson.objects.filter(
            tenant=tenant,
            is_active=True,
        ).order_by("family_name", "given_name")

        data = []
        for person in persons:
            data.append(
                {
                    "id": build_url(
                        request,
                        "session:person_detail",
                        tenant_slug=tenant.slug,
                        person_id=person.id,
                    ),
                    "type": "https://schema.oparl.org/1.1/Person",
                    "body": build_url(request, "session:oparl_body", tenant_slug=tenant.slug),
                    "name": person.display_name,
                    "familyName": person.family_name,
                    "givenName": person.given_name,
                    "title": person.title or None,
                    "formOfAddress": person.form_of_address or None,
                    "email": [person.email] if person.email else None,
                    "created": oparl_datetime(person.created_at),
                    "modified": oparl_datetime(person.updated_at),
                }
            )

        return self.oparl_json_response(
            {
                "data": data,
                "pagination": {
                    "totalElements": len(data),
                    "elementsPerPage": 100,
                    "currentPage": 1,
                    "totalPages": 1,
                },
                "links": {},
            }
        )


class OParlMeetingListView(OParlMixin, View):
    """OParl Meeting list endpoint (public meetings only)."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # Get public meetings
        meetings = (
            SessionMeeting.objects.filter(
                tenant=tenant,
                is_public=True,
            )
            .select_related("organization")
            .order_by("-start")[:100]
        )

        data = []
        for meeting in meetings:
            data.append(
                {
                    "id": build_url(
                        request,
                        "session:meeting_detail",
                        tenant_slug=tenant.slug,
                        meeting_id=meeting.id,
                    ),
                    "type": "https://schema.oparl.org/1.1/Meeting",
                    "name": meeting.name,
                    "meetingState": meeting.meeting_state,
                    "cancelled": meeting.cancelled,
                    "start": oparl_datetime(meeting.start),
                    "end": oparl_datetime(meeting.end),
                    "location": {
                        "description": meeting.location,
                        "room": meeting.room,
                        "streetAddress": meeting.street_address,
                        "postalCode": meeting.postal_code,
                        "locality": meeting.locality,
                    }
                    if meeting.location
                    else None,
                    "organization": [
                        build_url(
                            request,
                            "session:organization_detail",
                            tenant_slug=tenant.slug,
                            organization_id=meeting.organization_id,
                        )
                    ],
                    "created": oparl_datetime(meeting.created_at),
                    "modified": oparl_datetime(meeting.updated_at),
                }
            )

        return self.oparl_json_response(
            {
                "data": data,
                "pagination": {
                    "totalElements": SessionMeeting.objects.filter(tenant=tenant, is_public=True).count(),
                    "elementsPerPage": 100,
                    "currentPage": 1,
                    "totalPages": 1,
                },
                "links": {},
            }
        )


class OParlPaperListView(OParlMixin, View):
    """OParl Paper list endpoint (public papers only)."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # Get public papers
        papers = (
            SessionPaper.objects.filter(
                tenant=tenant,
                is_public=True,
            )
            .select_related("main_organization", "originator_organization")
            .order_by("-date", "-created_at")[:100]
        )

        data = []
        for paper in papers:
            data.append(
                {
                    "id": build_url(
                        request,
                        "session:paper_detail",
                        tenant_slug=tenant.slug,
                        paper_id=paper.id,
                    ),
                    "type": "https://schema.oparl.org/1.1/Paper",
                    "body": build_url(request, "session:oparl_body", tenant_slug=tenant.slug),
                    "name": paper.name,
                    "reference": paper.reference,
                    "date": oparl_date(paper.date),
                    "paperType": paper.get_paper_type_display(),
                    "mainFile": None,  # TODO: Add file reference
                    "created": oparl_datetime(paper.created_at),
                    "modified": oparl_datetime(paper.updated_at),
                }
            )

        return self.oparl_json_response(
            {
                "data": data,
                "pagination": {
                    "totalElements": SessionPaper.objects.filter(tenant=tenant, is_public=True).count(),
                    "elementsPerPage": 100,
                    "currentPage": 1,
                    "totalPages": 1,
                },
                "links": {},
            }
        )


# =============================================================================
# SESSION API (EXTENDED, AUTHENTICATED)
# =============================================================================


class SessionAPIMixin(OParlMixin):
    """Mixin for authenticated Session API views."""

    def get_session_user(self, request, tenant: SessionTenant):
        """Get session user for authenticated requests."""
        from apps.session.models import SessionUser

        if not request.user.is_authenticated:
            return None

        try:
            return SessionUser.objects.get(
                user=request.user,
                tenant=tenant,
                is_active=True,
            )
        except SessionUser.DoesNotExist:
            return None

    def check_permission(self, session_user, permission: str) -> bool:
        """Check if user has permission."""
        if not session_user:
            return False
        return SessionPermissionChecker(session_user).has_permission(permission)


class SessionMeetingListAPIView(SessionAPIMixin, View):
    """Session Meeting API - includes non-public meetings for authorized users."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)
        session_user = self.get_session_user(request, tenant)

        # Determine what meetings to show
        if session_user and self.check_permission(session_user, "view_non_public_meetings"):
            meetings = SessionMeeting.objects.filter(tenant=tenant)
        else:
            meetings = SessionMeeting.objects.filter(tenant=tenant, is_public=True)

        meetings = meetings.select_related("organization").order_by("-start")[:100]

        data = []
        for meeting in meetings:
            item = {
                "id": str(meeting.id),
                "name": meeting.name,
                "organization": {
                    "id": str(meeting.organization_id),
                    "name": meeting.organization.name,
                },
                "start": oparl_datetime(meeting.start),
                "end": oparl_datetime(meeting.end),
                "location": meeting.location,
                "meeting_state": meeting.meeting_state,
                "cancelled": meeting.cancelled,
                "is_public": meeting.is_public,
            }

            # Add non-public fields for authorized users
            if session_user and self.check_permission(session_user, "view_non_public_meetings"):
                if not meeting.is_public:
                    item["internal_notes"] = (
                        meeting.get_internal_notes_decrypted()
                        if hasattr(meeting, "get_internal_notes_decrypted")
                        else None
                    )

            data.append(item)

        return self.json_response(
            {
                "data": data,
                "meta": {
                    "total": len(data),
                    "authenticated": session_user is not None,
                },
            }
        )


class SessionPaperListAPIView(SessionAPIMixin, View):
    """Session Paper API - includes non-public papers for authorized users."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)
        session_user = self.get_session_user(request, tenant)

        # Determine what papers to show
        if session_user and self.check_permission(session_user, "view_non_public_papers"):
            papers = SessionPaper.objects.filter(tenant=tenant)
        else:
            papers = SessionPaper.objects.filter(tenant=tenant, is_public=True)

        papers = papers.select_related("main_organization", "originator_organization").order_by("-date", "-created_at")[
            :100
        ]

        data = []
        for paper in papers:
            item = {
                "id": str(paper.id),
                "reference": paper.reference,
                "name": paper.name,
                "paper_type": paper.paper_type,
                "status": paper.status,
                "date": oparl_date(paper.date),
                "is_public": paper.is_public,
                "main_organization": {
                    "id": str(paper.main_organization_id),
                    "name": paper.main_organization.name,
                }
                if paper.main_organization
                else None,
            }

            # Add non-public content for authorized users
            if session_user and self.check_permission(session_user, "view_non_public_papers"):
                item["main_text"] = paper.main_text
                item["resolution_text"] = paper.resolution_text

            data.append(item)

        return self.json_response(
            {
                "data": data,
                "meta": {
                    "total": len(data),
                    "authenticated": session_user is not None,
                },
            }
        )


class SessionApplicationListAPIView(SessionAPIMixin, View):
    """Session Application API - for viewing submitted applications."""

    def get(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)
        session_user = self.get_session_user(request, tenant)

        if not session_user or not self.check_permission(session_user, "view_applications"):
            return self.json_response({"error": "Unauthorized"}, status=403)

        applications = (
            SessionApplication.objects.filter(tenant=tenant)
            .select_related("submitting_organization", "target_organization")
            .order_by("-submitted_at")[:100]
        )

        data = []
        for app in applications:
            data.append(
                {
                    "id": str(app.id),
                    "reference": app.reference,
                    "title": app.title,
                    "application_type": app.application_type,
                    "status": app.status,
                    "submitter_name": app.submitter_name,
                    "submitting_organization": {
                        "id": str(app.submitting_organization_id),
                        "name": app.submitting_organization.name,
                    }
                    if app.submitting_organization
                    else None,
                    "target_organization": {
                        "id": str(app.target_organization_id),
                        "name": app.target_organization.name,
                    }
                    if app.target_organization
                    else None,
                    "is_urgent": app.is_urgent,
                    "submitted_at": oparl_datetime(app.submitted_at),
                }
            )

        return self.json_response(
            {
                "data": data,
                "meta": {
                    "total": len(data),
                },
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class ApplicationSubmitAPIView(SessionAPIMixin, View):
    """
    API endpoint for submitting applications from Work module.

    This allows political organizations to submit applications (Anträge)
    directly via API without document uploads.

    Authentication: Requires API token in Authorization header.
    Format: Authorization: Bearer <token>
    """

    def _authenticate_token(self, request, tenant: SessionTenant) -> SessionAPIToken | None:
        """Authenticate request using API token."""
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")

        if not auth_header.startswith("Bearer "):
            return None

        raw_token = auth_header[7:]  # Remove "Bearer " prefix

        if not raw_token or len(raw_token) != 64:
            return None

        # Hash the token and look it up
        hashed = SessionAPIToken.hash_token(raw_token)

        try:
            token = SessionAPIToken.objects.get(
                token=hashed,
                tenant=tenant,
            )
        except SessionAPIToken.DoesNotExist:
            return None

        # Check if token is valid
        if not token.is_valid():
            return None

        # Check IP restrictions
        client_ip = get_client_ip(request)
        if not token.check_ip(client_ip):
            return None

        return token

    def post(self, request, tenant_slug: str):
        tenant = self.get_tenant(tenant_slug)

        # Authenticate using API token
        api_token = self._authenticate_token(request, tenant)
        if not api_token:
            return self.json_response(
                {
                    "error": "Unauthorized",
                    "message": "Valid API token required. Use Authorization: Bearer <token>",
                },
                status=401,
            )

        # Check permission
        if not api_token.can_submit_applications:
            return self.json_response(
                {
                    "error": "Forbidden",
                    "message": "This token does not have permission to submit applications",
                },
                status=403,
            )

        # Parse JSON body
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return self.json_response({"error": "Invalid JSON"}, status=400)

        # Validate and sanitize required fields
        required_fields = [
            "title",
            "justification",
            "resolution_proposal",
            "submitter_name",
            "submitter_email",
        ]

        # Input validation - limit field lengths to prevent abuse
        max_lengths = {
            "title": 500,
            "justification": 50000,
            "resolution_proposal": 50000,
            "financial_impact": 10000,
            "submitter_name": 200,
            "submitter_email": 254,
            "submitter_phone": 50,
            "co_signers": 5000,
            "urgency_reason": 5000,
        }

        for field in required_fields:
            value = data.get(field, "")
            if not value:
                return self.json_response(
                    {"error": f"Missing required field: {field}"},
                    status=400,
                )
            if len(str(value)) > max_lengths.get(field, 10000):
                return self.json_response(
                    {"error": f"Field '{field}' exceeds maximum length"},
                    status=400,
                )

        # Validate email format (basic validation)
        submitter_email = data.get("submitter_email", "")
        if "@" not in submitter_email or "." not in submitter_email:
            return self.json_response(
                {"error": "Invalid email format"},
                status=400,
            )

        # Validate application type
        valid_types = ["motion", "inquiry", "proposal", "resolution", "urgent_motion"]
        application_type = data.get("application_type", "motion")
        if application_type not in valid_types:
            return self.json_response(
                {"error": f"Invalid application_type. Valid types: {', '.join(valid_types)}"},
                status=400,
            )

        # Get submitting organization (from Work module) - optional
        submitting_org = None
        if data.get("submitting_organization_id"):
            try:
                from apps.tenants.models import Organization

                submitting_org = Organization.objects.get(id=data["submitting_organization_id"])
            except (Organization.DoesNotExist, ImportError):
                pass

        # Get target organization - optional
        target_org = None
        if data.get("target_organization_id"):
            try:
                target_org = SessionOrganization.objects.get(
                    id=data["target_organization_id"],
                    tenant=tenant,
                )
            except SessionOrganization.DoesNotExist:
                pass

        # Create application
        application = SessionApplication.objects.create(
            tenant=tenant,
            title=data["title"][: max_lengths["title"]],
            application_type=application_type,
            justification=data["justification"][: max_lengths["justification"]],
            resolution_proposal=data["resolution_proposal"][: max_lengths["resolution_proposal"]],
            financial_impact=data.get("financial_impact", "")[: max_lengths["financial_impact"]],
            submitting_organization=submitting_org,
            submitter_name=data["submitter_name"][: max_lengths["submitter_name"]],
            submitter_email=submitter_email[: max_lengths["submitter_email"]],
            submitter_phone=data.get("submitter_phone", "")[: max_lengths["submitter_phone"]],
            co_signers=data.get("co_signers", "")[: max_lengths["co_signers"]],
            target_organization=target_org,
            is_urgent=bool(data.get("is_urgent", False)),
            urgency_reason=data.get("urgency_reason", "")[: max_lengths["urgency_reason"]],
            deadline=data.get("deadline"),
        )

        # Record token usage
        api_token.record_usage(get_client_ip(request))

        return self.json_response(
            {
                "success": True,
                "application": {
                    "id": str(application.id),
                    "reference": application.reference,
                    "status": application.status,
                },
            },
            status=201,
        )
