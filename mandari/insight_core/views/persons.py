# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone
from django.views.generic import DetailView, ListView

from ..models import (
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    PublicQuestion,
)
from ._helpers import get_active_body

# =============================================================================
# Personen
# =============================================================================


COUNCIL_ROLES = [
    "Ratsmitglied",
    "Oberbürgermeister",
    "Bürgermeister/in",
    "Fraktionsvorsitzende/r Rat",
]


class PersonListView(ListView):
    """Liste aller Personen mit Ratsrolle-Annotation."""

    model = OParlPerson
    template_name = "pages/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["partials/person_list_items.html"]
        return [self.template_name]

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPerson.objects.none()

        today = timezone.now().date()

        qs = OParlPerson.objects.filter(body=body).select_related("body")

        # Ratsrolle als Annotation (falls vorhanden)
        rat = OParlOrganization.objects.filter(body=body, name="Rat").first()
        if rat:
            council_role_sq = Subquery(
                OParlMembership.objects.filter(
                    person=OuterRef("pk"),
                    organization=rat,
                    role__in=COUNCIL_ROLES,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .values("role")[:1]
            )
            qs = qs.annotate(council_role=council_role_sq)

        # Suche (Name + Funktion/Gremium über Mitgliedschaften)
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(family_name__icontains=q)
                | Q(given_name__icontains=q)
                | Q(email__icontains=q)
                | Q(memberships__role__icontains=q)
                | Q(memberships__organization__name__icontains=q)
            ).distinct()

        return qs.order_by("family_name", "given_name")


class PersonDetailView(DetailView):
    """Detailseite einer Person."""

    model = OParlPerson
    template_name = "pages/persons/detail.html"
    context_object_name = "person"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object
        today = timezone.now().date()

        all_memberships = person.memberships.select_related("organization")
        context["active_memberships"] = all_memberships.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        ).order_by("organization__name")
        context["past_memberships"] = all_memberships.filter(end_date__lt=today).order_by("organization__name")

        # Ratsrolle ermitteln (für Hero-Anzeige)
        council_membership = (
            all_memberships.filter(
                organization__name="Rat",
                role__in=COUNCIL_ROLES,
            )
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .first()
        )
        context["council_role"] = council_membership.role if council_membership else None

        # Öffentliche Fragen (nur bei Ratsmitgliedern)
        if council_membership:
            published_questions = PublicQuestion.objects.filter(
                recipient=person,
                status="published",
            ).order_by("-created_at")[:20]
            context["published_questions"] = published_questions

            total = published_questions.count()
            answered = sum(1 for q in published_questions if q.answer_status == "published")
            context["answer_stats"] = {
                "total": total,
                "answered": answered,
            }

        # SEO-Kontext
        from ..seo import get_person_seo

        context["seo"] = get_person_seo(person, self.request).to_dict()

        return context


class PersonListPartial(ListView):
    """HTMX Partial für Personen-Liste."""

    model = OParlPerson
    template_name = "partials/person_list_items.html"
    context_object_name = "persons"
    paginate_by = 20
