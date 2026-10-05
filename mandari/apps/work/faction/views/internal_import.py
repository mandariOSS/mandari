# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Unterlage einlesen und TOPs übernehmen (Issue #873).

- ``FactionInternalImportView``: GET Formular, POST PDF hochladen → Datei in „Nichtöffentliche Vorgänge“
  ablegen, TOPs erkennen und gleich zur Bestätigung anzeigen.
- ``FactionInternalImportConfirmView``: GET Vorschläge einer abgelegten Unterlage erneut anzeigen, POST die
  ausgewählten Vorschläge als nichtöffentliche TOPs anlegen.

Nur vereidigte Mitglieder mit Genehmigungsrecht (``internal_documents.can_import``); alle anderen erhalten 403,
fremde oder nicht nichtöffentliche Unterlagen 404.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.views import View
from django.views.generic.base import ContextMixin

from apps.common.mixins import WorkViewMixin
from apps.common.uploads import MB, PDF, validate_upload
from apps.tenants.models import Membership, Organization
from apps.work.motions import non_public
from apps.work.motions.models import Motion

from .. import internal_documents
from ..models import FactionMeeting

logger = logging.getLogger(__name__)

#: Größte Unterlage (gescannte Sitzungsunterlagen sind selten größer)
MAX_BYTES = 25 * MB

UPLOAD_TEMPLATE = "work/faction/internal_import.html"
CONFIRM_TEMPLATE = "work/faction/internal_import_confirm.html"


class _InternalImportMixin(WorkViewMixin):
    permission_required = "faction.view_public"

    @property
    def _org(self) -> Organization:
        return cast(Organization, self.organization)

    @property
    def _member(self) -> Membership:
        return cast(Membership, self.membership)

    def _meeting(self, meeting_id: Any) -> FactionMeeting:
        meeting = get_object_or_404(FactionMeeting, id=meeting_id, organization=self._org)
        if not internal_documents.can_import(self._member, meeting):
            raise PermissionDenied(internal_documents.NOT_ALLOWED)
        return meeting

    def _not_editable(self, request: HttpRequest, meeting: FactionMeeting) -> HttpResponse:
        messages.error(request, internal_documents.NOT_EDITABLE)
        return redirect("work:faction_detail", org_slug=self._org.slug, meeting_id=meeting.id)

    def _render(self, request: HttpRequest, template: str, context: dict[str, Any], status: int = 200) -> HttpResponse:
        base = cast(dict[str, Any], cast(Any, self).get_context_data())
        return TemplateResponse(request, template, {**base, "active_nav": "faction", **context}, status=status)

    def _confirm(
        self,
        request: HttpRequest,
        meeting: FactionMeeting,
        motion: Motion,
        proposals: list[internal_documents.AgendaProposal],
        notice: str = "",
    ) -> HttpResponse:
        folder = motion.folder
        return self._render(
            request,
            CONFIRM_TEMPLATE,
            {
                "meeting": meeting,
                "motion": motion,
                "proposals": proposals,
                "notice": notice,
                "folder_name": folder.name if folder is not None else "",
            },
        )


class FactionInternalImportView(_InternalImportMixin, ContextMixin, View):
    """Formular und Upload: PDF ablegen, TOPs erkennen, Bestätigung anzeigen."""

    def _form(self, request: HttpRequest, meeting: FactionMeeting, error: str = "", status: int = 200) -> HttpResponse:
        context = {"meeting": meeting, "max_mb": MAX_BYTES // MB, "error": error}
        return self._render(request, UPLOAD_TEMPLATE, context, status=status)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = self._meeting(kwargs["meeting_id"])
        if not internal_documents.is_editable(meeting):
            return self._not_editable(request, meeting)
        return self._form(request, meeting)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = self._meeting(kwargs["meeting_id"])
        if not internal_documents.is_editable(meeting):
            return self._not_editable(request, meeting)

        uploads = request.FILES.getlist("datei")
        uploaded = uploads[0] if uploads else None
        try:
            validate_upload(uploaded, allowed=PDF, max_bytes=MAX_BYTES, bezeichnung="Datei")
        except ValidationError as exc:
            return self._form(request, meeting, " ".join(exc.messages), status=400)

        try:
            result = internal_documents.import_document(meeting, self._member, uploaded)
        except Exception:
            logger.exception("Nichtöffentliche Unterlage für Sitzung %s nicht eingelesen", meeting.id)
            result = internal_documents.ImportResult(error=non_public.UNREADABLE)
        if result.error or result.motion is None:
            return self._form(request, meeting, result.error or non_public.UNREADABLE, status=400)
        return self._confirm(request, meeting, result.motion, result.proposals or [], result.notice)


class FactionInternalImportConfirmView(_InternalImportMixin, ContextMixin, View):
    """Vorschläge einer abgelegten Unterlage anzeigen (GET) und als NÖ-TOPs übernehmen (POST)."""

    def _motion(self, motion_id: Any) -> Motion:
        motion = get_object_or_404(Motion, id=motion_id, organization=self._org)
        if not motion.is_sworn_in_only() or not motion.can_access(self._member):
            raise Http404("Unterlage nicht gefunden.")
        return motion

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = self._meeting(kwargs["meeting_id"])
        if not internal_documents.is_editable(meeting):
            return self._not_editable(request, meeting)
        motion = self._motion(kwargs["motion_id"])
        proposals, notice = internal_documents.proposals_for(motion)
        return self._confirm(request, meeting, motion, proposals, notice)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        meeting = self._meeting(kwargs["meeting_id"])
        if not internal_documents.is_editable(meeting):
            return self._not_editable(request, meeting)
        motion = self._motion(kwargs["motion_id"])

        selected = internal_documents.selected_proposals(
            numbers=request.POST.getlist("nr"),
            titles=request.POST.getlist("titel"),
            chosen=request.POST.getlist("auswahl"),
        )
        if not selected:
            messages.error(request, internal_documents.NOTHING_SELECTED)
            return redirect(
                "work:faction_internal_import_confirm",
                org_slug=self._org.slug,
                meeting_id=meeting.id,
                motion_id=motion.id,
            )
        created = internal_documents.confirm_items(meeting, self._member, motion, selected)
        messages.success(request, f"{len(created)} nichtöffentliche TOPs übernommen.")
        return redirect("work:faction_detail", org_slug=self._org.slug, meeting_id=meeting.id)
