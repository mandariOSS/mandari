# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView, View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..models import (
    Motion,
)


def _visible_trash(membership):
    """Gelöschte Dokumente, die das Mitglied sehen darf – nie fremde private."""
    return Motion.visible_to(membership, include_deleted=True).filter(status="deleted")


def _editable_trashed_motion(membership, motion_id):
    """Gelöschtes Dokument zum Wiederherstellen/Löschen; 404, wenn nicht sichtbar."""
    motion = get_object_or_404(_visible_trash(membership), id=motion_id)
    if not motion.can_edit(membership):
        raise PermissionDenied("Keine Berechtigung für dieses Dokument.")
    return motion


def _can_purge(membership, motion):
    """Endgültig löschen: nur Autor:in oder „alle Anträge bearbeiten" (wie Freigaben und Versionen)."""
    return motion.author_id == membership.id or membership.has_permission("motions.edit_all")


# =============================================================================
# Trash (Papierkorb) Views
# =============================================================================


class MotionTrashView(WorkViewMixin, TemplateView):
    """View deleted documents (Papierkorb)."""

    template_name = "work/motions/trash.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"

        # Get only deleted motions
        deleted_motions = _visible_trash(self.membership).select_related("author__user").order_by("-deleted_at")

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            deleted_motions = deleted_motions.filter(Q(title__icontains=search) | Q(summary__icontains=search))
            context["search_query"] = search

        # Pagination
        paginator = Paginator(deleted_motions, 20)
        page = self.request.GET.get("page", 1)
        context["motions"] = paginator.get_page(page)
        context["paginator"] = paginator
        context["trash_count"] = _visible_trash(self.membership).count()

        return context


class MotionRestoreView(WorkViewMixin, View):
    """Restore a motion from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = _editable_trashed_motion(self.membership, kwargs.get("motion_id"))

        # Restore to draft
        motion.status = "draft"
        motion.deleted_at = None
        motion.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        messages.success(request, f"'{motion.title}' wurde wiederhergestellt.")
        return redirect("work:document_trash", org_slug=self.organization.slug)


class MotionPermanentDeleteView(WorkViewMixin, View):
    """Permanently delete a motion from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = _editable_trashed_motion(self.membership, kwargs.get("motion_id"))

        if not _can_purge(self.membership, motion):
            raise PermissionDenied("Endgültig löschen dürfen nur Autor:in oder Berechtigte für alle Anträge.")

        title = motion.title
        motion.delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        messages.success(request, f"'{title}' wurde endgültig gelöscht.")
        return redirect("work:document_trash", org_slug=self.organization.slug)


class MotionEmptyTrashView(WorkViewMixin, View):
    """Empty all items from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        # Nur, was das Mitglied endgültig löschen darf – nie fremde Dokumente
        ids = [motion.pk for motion in _visible_trash(self.membership) if _can_purge(self.membership, motion)]
        count = len(ids)
        Motion.objects.filter(pk__in=ids).delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "count": count})

        messages.success(request, f"Papierkorb geleert ({count} Dokumente gelöscht).")
        return redirect("work:document_trash", org_slug=self.organization.slug)
