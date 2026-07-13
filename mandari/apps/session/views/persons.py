# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.db.models import Q
from django.views.generic import (
    DetailView,
    ListView,
)

from ..models import (
    SessionPerson,
)
from ..permissions import SessionViewMixin

# =============================================================================
# PERSONS
# =============================================================================


class PersonListView(SessionViewMixin, ListView):
    """List of persons."""

    model = SessionPerson
    template_name = "session/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50
    permission_required = "view_meetings"  # Basic access

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.order_by("family_name", "given_name")

        # Filter by active status
        if self.request.GET.get("active") != "0":
            qs = qs.filter(is_active=True)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(given_name__icontains=search) | Q(family_name__icontains=search) | Q(email__icontains=search)
            )

        return qs


class PersonDetailView(SessionViewMixin, DetailView):
    """Person detail view."""

    model = SessionPerson
    template_name = "session/persons/detail.html"
    context_object_name = "person"
    pk_url_kwarg = "person_id"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object

        # Memberships
        context["memberships"] = person.memberships.select_related("organization").order_by("-start_date")

        # Recent attendances
        context["recent_attendances"] = person.attendances.select_related("meeting__organization").order_by(
            "-meeting__start"
        )[:10]

        return context
