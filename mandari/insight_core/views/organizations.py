"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone
from django.views.generic import DetailView, ListView

from ..models import (
    OParlMeeting,
    OParlOrganization,
)
from ..ranking import sort_organizations_by_ranking
from ._helpers import get_active_body

# =============================================================================
# Gremien (Organizations)
# =============================================================================


class OrganizationListView(ListView):
    """Liste aller Gremien mit Aktiv/Alle-Tabs."""

    model = OParlOrganization
    template_name = "pages/organizations/list.html"
    context_object_name = "organizations"
    paginate_by = 50

    def get_template_names(self):
        # Für HTMX-Requests nur das Partial zurückgeben
        if self.request.headers.get("HX-Request"):
            return ["partials/organization_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlOrganization.objects.none()

        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()
        today = timezone.now().date()
        now = timezone.now()

        # Annotate next/last meeting via M2M Subquery (fast, uses proper indexes)
        next_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__gte=now,
                cancelled=False,
            )
            .order_by("start")
            .values("start")[:1]
        )
        last_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__lt=now,
            )
            .order_by("-start")
            .values("start")[:1]
        )
        has_any_meeting = Exists(OParlMeeting.objects.filter(organizations=OuterRef("pk")))

        base_qs = OParlOrganization.objects.filter(body=body).annotate(
            next_meeting=next_meeting_sq,
            last_meeting=last_meeting_sq,
            has_meetings=has_any_meeting,
        )

        # Suche
        if q:
            base_qs = base_qs.filter(Q(name__icontains=q) | Q(short_name__icontains=q))

        if tab == "active":
            # Aktiv = nicht abgelaufen UND hat mindestens eine Sitzung
            base_qs = base_qs.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today),
                has_meetings=True,
            )

        return sort_organizations_by_ranking(base_qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        tab = self.request.GET.get("tab", "active")

        if body:
            today = timezone.now().date()
            has_any_meeting = Exists(OParlMeeting.objects.filter(organizations=OuterRef("pk")))

            # Counts ohne Suchfilter
            all_orgs = OParlOrganization.objects.filter(body=body).annotate(
                has_meetings=has_any_meeting,
            )
            context["active_count"] = all_orgs.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today),
                has_meetings=True,
            ).count()
            context["all_count"] = all_orgs.count()

        context["tab"] = tab
        return context


class OrganizationDetailView(DetailView):
    """Detailseite eines Gremiums."""

    model = OParlOrganization
    template_name = "pages/organizations/detail.html"
    context_object_name = "organization"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object
        today = timezone.now().date()
        now = timezone.now()

        all_memberships = org.memberships.select_related("person", "person__body")
        active_qs = all_memberships.filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).order_by(
            "person__family_name"
        )
        past_qs = all_memberships.filter(end_date__lt=today).order_by("person__family_name")

        # Sonderfall "Rat": Ratsmitglieder von anderen trennen
        is_rat = org.name == "Rat"
        context["is_rat"] = is_rat

        if is_rat:
            council_roles = [
                "Ratsmitglied",
                "Oberbürgermeister",
                "Bürgermeister/in",
                "Fraktionsvorsitzende/r Rat",
            ]
            context["council_members"] = active_qs.filter(role__in=council_roles)
            context["other_members"] = active_qs.exclude(role__in=council_roles)
        else:
            context["active_members"] = active_qs

        context["past_members"] = past_qs

        # Sitzungen
        context["upcoming_meetings"] = OParlMeeting.objects.filter(
            organizations=org,
            start__gte=now,
            cancelled=False,
        ).order_by("start")[:10]
        context["past_meetings"] = OParlMeeting.objects.filter(
            organizations=org,
            start__lt=now,
        ).order_by("-start")[:10]

        # SEO-Kontext
        from ..seo import get_organization_seo

        context["seo"] = get_organization_seo(org, self.request).to_dict()

        return context


class OrganizationListPartial(ListView):
    """HTMX Partial für Gremien-Liste."""

    model = OParlOrganization
    template_name = "partials/organization_list.html"
    context_object_name = "organizations"
    paginate_by = 20

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlOrganization.objects.none()

        tab = self.request.GET.get("tab", "active")
        today = timezone.now().date()
        base_qs = OParlOrganization.objects.filter(body=body)

        if tab == "active":
            qs = base_qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        else:
            qs = base_qs.filter(end_date__lt=today)
        return sort_organizations_by_ranking(qs)
