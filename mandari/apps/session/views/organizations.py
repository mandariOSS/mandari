# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.db.models import Count, Q
from django.views.generic import (
    DetailView,
    ListView,
)

from ..models import (
    SessionOrganization,
    SessionPaper,
)
from ..permissions import SessionViewMixin

# =============================================================================
# ORGANIZATIONS
# =============================================================================


class OrganizationListView(SessionViewMixin, ListView):
    """List of organizations/committees."""

    model = SessionOrganization
    template_name = "session/organizations/list.html"
    context_object_name = "organizations"
    paginate_by = 20
    permission_required = "view_meetings"  # Anyone who can view meetings can see orgs

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.annotate(member_count=Count("memberships", filter=Q(memberships__end_date__isnull=True))).order_by(
            "name"
        )

        # Filter by type
        org_type = self.request.GET.get("type")
        if org_type:
            qs = qs.filter(organization_type=org_type)

        # Filter by active status
        if self.request.GET.get("active") == "1":
            qs = qs.filter(is_active=True)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organization_types"] = SessionOrganization._meta.get_field("organization_type").choices
        return context


class OrganizationDetailView(SessionViewMixin, DetailView):
    """Organization detail view."""

    model = SessionOrganization
    template_name = "session/organizations/detail.html"
    context_object_name = "organization"
    pk_url_kwarg = "organization_id"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object

        # Members
        context["memberships"] = (
            org.memberships.select_related("person").filter(end_date__isnull=True).order_by("person__family_name")
        )

        # Recent meetings
        context["recent_meetings"] = org.meetings.order_by("-start")[:5]

        # Recent papers
        context["recent_papers"] = SessionPaper.objects.filter(
            Q(main_organization=org) | Q(originator_organization=org)
        ).order_by("-date")[:5]

        return context
