# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endpunkte der Session-API v1 unter ``/api/v1/session/{tenant_slug}/``.

Fachlich identisch mit den bisherigen Views unter ``/session/<slug>/api/session/…`` (die bleiben bis
zur angekündigten Abschaltung mit ``Deprecation``-Header erreichbar), zusätzlich: OpenAPI-Schema,
``limit``/``offset``, RFC-9457-Fehler. API-Token lesen nur Öffentliches; Nichtöffentliches und interne
Notizen gibt es nur für angemeldete Personen mit dem NÖ-Recht (``auth.TOKEN_PERMISSIONS``).
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from ninja import Query, Router

from apps.session.models import (
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)

from .auth import BearerOrSession, Principal, client_ip, resolve_principal
from .problems import Problem
from .schemas import (
    ApplicationCreated,
    ApplicationFeedbackOut,
    ApplicationIn,
    ApplicationList,
    ListMeta,
    MeetingList,
    PaperList,
    TenantRoot,
)

router = Router(auth=BearerOrSession(), tags=["Session"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 200


def get_tenant(tenant_slug: str) -> SessionTenant:
    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True).first()
    if tenant is None:
        raise Problem(404, f"Mandant „{tenant_slug}“ nicht gefunden.", kind="nicht-gefunden")
    return tenant


def _page(qs: QuerySet[Any], limit: int, offset: int) -> tuple[list[Any], int]:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    total = qs.count()
    return list(qs[offset : offset + limit]), total


def _meta(total: int, limit: int, offset: int, principal: Principal) -> ListMeta:
    return ListMeta(
        total=total, limit=max(1, min(limit, MAX_LIMIT)), offset=max(0, offset), authenticated=principal.authenticated
    )


def _org(obj: Any) -> dict[str, Any] | None:
    return {"id": obj.id, "name": obj.name} if obj is not None else None


# ---------------------------------------------------------------------------


@router.get("/{tenant_slug}/", response=TenantRoot, summary="Einstiegspunkt je Mandant")
def tenant_root(request: HttpRequest, tenant_slug: str) -> dict[str, Any]:
    tenant = get_tenant(tenant_slug)
    base = request.build_absolute_uri
    return {
        "name": f"mandari Session-API – {tenant.name}",
        "version": "1",
        "tenant": tenant.slug,
        "oparl": base(reverse("session:oparl_system", kwargs={"tenant_slug": tenant.slug})),
        "meetings": base(reverse("session_api_v1:meetings", kwargs={"tenant_slug": tenant.slug})),
        "papers": base(reverse("session_api_v1:papers", kwargs={"tenant_slug": tenant.slug})),
        "applications": base(reverse("session_api_v1:applications", kwargs={"tenant_slug": tenant.slug})),
        "submit_application": base(reverse("session_api_v1:submit_application", kwargs={"tenant_slug": tenant.slug})),
        "openapi": base(reverse("session_api_v1:openapi-json")),
        "docs": base(reverse("session_api_v1:openapi-view")),
    }


@router.get("/{tenant_slug}/meetings/", response=MeetingList, url_name="meetings", summary="Sitzungen")
def meetings(
    request: HttpRequest,
    tenant_slug: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Öffentliche Sitzungen für alle; nicht-öffentliche mit Recht ``view_non_public_meetings``."""
    tenant = get_tenant(tenant_slug)
    principal = resolve_principal(request, tenant)
    non_public = principal.has_permission("view_non_public_meetings")
    qs = SessionMeeting.objects.filter(tenant=tenant)
    if not non_public:
        qs = qs.filter(is_public=True)
    rows, total = _page(
        qs.select_related("organization").prefetch_related("joint_organizations").order_by("-start"), limit, offset
    )
    data = []
    for meeting in rows:
        item: dict[str, Any] = {
            "id": meeting.id,
            "name": meeting.name,
            "organization": _org(meeting.organization),
            "joint_organizations": [_org(org) for org in meeting.joint_organization_list],
            "start": meeting.start,
            "end": meeting.end,
            "location": meeting.location or "",
            "meeting_state": meeting.meeting_state or "",
            "cancelled": meeting.cancelled,
            "is_public": meeting.is_public,
        }
        # Interne Notizen nur für angemeldete Personen mit NÖ-Recht, nie für API-Token
        if (
            non_public
            and principal.session_user is not None
            and not meeting.is_public
            and hasattr(meeting, "get_internal_notes_decrypted")
        ):
            item["internal_notes"] = meeting.get_internal_notes_decrypted()
        data.append(item)
    return {"data": data, "meta": _meta(total, limit, offset, principal)}


@router.get("/{tenant_slug}/papers/", response=PaperList, url_name="papers", summary="Vorlagen")
def papers(
    request: HttpRequest,
    tenant_slug: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Öffentliche Vorlagen für alle; nicht-öffentliche samt Texten mit Recht ``view_non_public_papers``."""
    tenant = get_tenant(tenant_slug)
    principal = resolve_principal(request, tenant)
    non_public = principal.has_permission("view_non_public_papers")
    qs = SessionPaper.objects.filter(tenant=tenant)
    if not non_public:
        qs = qs.filter(is_public=True)
    rows, total = _page(
        qs.select_related("main_organization", "originator_organization").order_by("-date", "-created_at"),
        limit,
        offset,
    )
    data = []
    for paper in rows:
        item: dict[str, Any] = {
            "id": paper.id,
            "reference": paper.reference,
            "name": paper.name,
            "paper_type": paper.paper_type or "",
            "status": paper.status or "",
            "date": paper.date,
            "is_public": paper.is_public,
            "main_organization": _org(paper.main_organization),
        }
        if non_public:
            item["main_text"] = paper.main_text
            item["resolution_text"] = paper.resolution_text
        data.append(item)
    return {"data": data, "meta": _meta(total, limit, offset, principal)}


@router.get("/{tenant_slug}/applications/", response=ApplicationList, url_name="applications", summary="Anträge")
def applications(
    request: HttpRequest,
    tenant_slug: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Eingereichte Anträge – nur mit Recht ``view_applications`` (Sitzung) bzw. Token-Flag."""
    tenant = get_tenant(tenant_slug)
    principal = resolve_principal(request, tenant)
    principal.require("view_applications", "Anträge sind nur mit dem Recht „Anträge anzeigen“ abrufbar.")
    qs = (
        SessionApplication.objects.filter(tenant=tenant)
        .select_related("submitting_organization", "target_organization")
        .order_by("-submitted_at")
    )
    rows, total = _page(qs, limit, offset)
    data = [
        {
            "id": app.id,
            "reference": app.reference,
            "title": app.title,
            "application_type": app.application_type,
            "status": app.status,
            "submitter_name": app.submitter_name,
            "submitting_organization": _org(app.submitting_organization),
            "target_organization": _org(app.target_organization),
            "is_urgent": app.is_urgent,
            "submitted_at": app.submitted_at,
        }
        for app in rows
    ]
    return {"data": data, "meta": _meta(total, limit, offset, principal)}


@router.post(
    "/{tenant_slug}/applications/submit/",
    response={201: ApplicationCreated},
    url_name="submit_application",
    summary="Antrag einreichen (API-Token)",
)
def submit_application(request: HttpRequest, tenant_slug: str, payload: ApplicationIn) -> tuple[int, dict[str, Any]]:
    """Legt einen Antrag an. Erfordert ein API-Token des Mandanten mit ``can_submit_applications``."""
    tenant = get_tenant(tenant_slug)
    principal = resolve_principal(request, tenant)
    if principal.token is None:
        raise Problem(
            401,
            "Zum Einreichen ist ein API-Token erforderlich (Authorization: Bearer <token>).",
            kind="nicht-authentifiziert",
        )
    if not principal.has_permission("submit_applications"):
        raise Problem(403, "Dieses Token darf keine Anträge einreichen.", kind="keine-berechtigung")

    from apps.session.services.application_service import (
        SUBMITTING_ORGANIZATION_MISMATCH,
        SubmittingOrganizationMismatchError,
        submitting_organization_for_token,
    )

    try:
        submitting_org = submitting_organization_for_token(principal.token, payload.submitting_organization_id)
    except SubmittingOrganizationMismatchError as exc:
        raise Problem(403, SUBMITTING_ORGANIZATION_MISMATCH, kind="keine-berechtigung") from exc
    target_org = None
    if payload.target_organization_id:
        with contextlib.suppress(SessionOrganization.DoesNotExist):
            target_org = SessionOrganization.objects.get(id=payload.target_organization_id, tenant=tenant)

    application = SessionApplication.objects.create(
        tenant=tenant,
        title=payload.title,
        application_type=payload.application_type,
        justification=payload.justification,
        resolution_proposal=payload.resolution_proposal,
        financial_impact=payload.financial_impact,
        submitting_organization=submitting_org,
        submitter_name=payload.submitter_name,
        submitter_email=payload.submitter_email,
        submitter_phone=payload.submitter_phone,
        co_signers=payload.co_signers,
        target_organization=target_org,
        is_urgent=payload.is_urgent,
        urgency_reason=payload.urgency_reason,
        deadline=payload.deadline,
        submitted_via_token=principal.token,
    )
    principal.token.record_usage(client_ip(request))
    feedback_url = request.build_absolute_uri(
        reverse(
            "session_api_v1:application_feedback",
            kwargs={"tenant_slug": tenant.slug, "application_id": application.id},
        )
    )
    return 201, {
        "id": application.id,
        "reference": application.reference,
        "status": application.status,
        "feedback": feedback_url,
    }


@router.get(
    "/{tenant_slug}/applications/{application_id}/feedback/",
    response=ApplicationFeedbackOut,
    url_name="application_feedback",
    summary="Rückmeldestand eines eingereichten Antrags (API-Token)",
    exclude_none=True,
)
def application_feedback(request: HttpRequest, tenant_slug: str, application_id: UUID) -> dict[str, Any]:
    """
    Eingang, Vorlagennummer, Beratungsfolge und Beschluss eines eingereichten Antrags (Issue #316).

    Nur mit dem API-Token, mit dem der Antrag eingereicht wurde, oder einem Token, das dieselbe
    Organisation in mandari Work verbunden hat. Fremde Anträge sind nicht auffindbar (404). Es gelten
    die Ö/NÖ-Regeln der OParl-Schnittstelle: Nicht-öffentliche Stationen erscheinen nur als
    „nicht-öffentlich beraten“.
    """
    from apps.session.services import application_feedback as feedback_service

    tenant = get_tenant(tenant_slug)
    principal = resolve_principal(request, tenant)
    if principal.token is None:
        raise Problem(
            401,
            "Der Rückmeldestand ist nur mit dem API-Token abrufbar, mit dem der Antrag eingereicht wurde.",
            kind="nicht-authentifiziert",
        )
    if not principal.has_permission("submit_applications"):
        raise Problem(403, "Dieses Token darf keine Anträge einreichen.", kind="keine-berechtigung")
    application = feedback_service.application_for_token(principal.token, application_id)
    if application is None:
        raise Problem(404, "Antrag nicht gefunden.", kind="nicht-gefunden")
    principal.token.record_usage(client_ip(request))
    return feedback_service.build(application).as_dict()


__all__ = ["router", "UUID"]
