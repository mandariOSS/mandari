# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Q
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin


class RISFilesView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS files/documents list."""

    template_name = "work/ris/files.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_files"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlFile
        from insight_core.views import _annotate_files_with_context

        # Base queryset
        files = OParlFile.objects.filter(body__in=bodies).select_related("paper").order_by("-file_date", "-created_at")

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            files = files.filter(
                Q(name__icontains=search) | Q(file_name__icontains=search) | Q(paper__name__icontains=search)
            )
            context["search_query"] = search

        # Pagination
        paginator = Paginator(files, 30)
        page = self.request.GET.get("page", 1)
        page_obj = paginator.get_page(page)

        # Annotate with context (organization, meeting, agenda item)
        _annotate_files_with_context(page_obj.object_list)

        context["files"] = page_obj
        context["paginator"] = paginator
        context["total_count"] = paginator.count

        return context
