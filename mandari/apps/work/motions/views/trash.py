# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.contrib import messages
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
        deleted_motions = (
            Motion.objects.filter(organization=self.organization, status="deleted")
            .select_related("author__user")
            .order_by("-deleted_at")
        )

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
        context["trash_count"] = Motion.objects.filter(organization=self.organization, status="deleted").count()

        return context


class MotionRestoreView(WorkViewMixin, View):
    """Restore a motion from trash."""

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization, status="deleted")

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
        motion = get_object_or_404(Motion, id=kwargs.get("motion_id"), organization=self.organization, status="deleted")

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
        count = Motion.objects.filter(organization=self.organization, status="deleted").count()

        Motion.objects.filter(organization=self.organization, status="deleted").delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "count": count})

        messages.success(request, f"Papierkorb geleert ({count} Dokumente gelöscht).")
        return redirect("work:document_trash", org_slug=self.organization.slug)
