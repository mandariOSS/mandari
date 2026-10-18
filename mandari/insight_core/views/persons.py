# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.utils import timezone
from django.views.generic import DetailView, ListView

from apps.common.mixins import HTMXMixin

from ..models import (
    OParlMeeting,
    OParlPerson,
    PublicQuestion,
    withdrawn_q,
)
from ..services import fraktionen, personen_liste
from ._helpers import ActiveBodyRequiredMixin, get_active_body
from ._withdrawn import withdrawn_response

# =============================================================================
# Personen
# =============================================================================


COUNCIL_ROLES = [
    "Ratsmitglied",
    "Oberbürgermeister",
    "Bürgermeister/in",
    "Fraktionsvorsitzende/r Rat",
]


class PersonListView(HTMXMixin, ActiveBodyRequiredMixin, ListView):
    """Liste aller Personen mit Ratsrolle-Annotation."""

    model = OParlPerson
    template_name = "pages/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50

    def get_template_names(self):
        if self.is_htmx:
            return ["partials/person_list_items.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Personen & Ratsmitglieder",
            description="Ratsmitglieder und Beteiligte der Kommunalpolitik: Wer sitzt in welchem Gremium und trifft Entscheidungen?",
            body=get_active_body(self.request),
        ).to_dict()
        # Fraktion, Funktion und Gremien aus den laufenden Mitgliedschaften (Issue #841), eine Abfrage je Seite
        persons = list(context["persons"])
        angaben = personen_liste.angaben_fuer(persons)
        for person in persons:
            person.angaben = angaben.get(person.pk) or personen_liste.PersonAngaben()
        context["persons"] = persons
        return context

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return OParlPerson.objects.none()

        qs = OParlPerson.objects.filter(body=body, deleted=False).select_related("body")

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

    def get(self, request, *args, **kwargs):
        # Von mandari Session zurückgenommen (auf Antrag gelöscht, Mandant deaktiviert): kein Inhalt
        self.object = self.get_object()
        if self.object.withdrawn_by_publisher:
            return withdrawn_response(request, self.object)
        return self.render_to_response(self.get_context_data(object=self.object))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object
        # Ortszeit wie die Sitemap (sitemaps.py) – timezone.now().date() wäre das UTC-Datum und läge
        # zwischen 0 und 2 Uhr einen Tag zurück (ausgeschiedene Personen galten dann noch als laufend)
        today = timezone.localdate()

        # Nur bestehende Mitgliedschaften in bestehenden, nicht zurückgenommenen Gremien (gelöschte Fraktionen
        # erschienen sonst als laufende Mitgliedschaft, Prüfung #848)
        all_memberships = (
            person.memberships.filter(deleted=False, organization__deleted=False)
            .exclude(withdrawn_q("organization"))
            .select_related("organization")
        )
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
        # Wichtigste laufende Rolle für den Kopf (wie die Personenliste, Issue #841)
        context["funktion"] = personen_liste.funktion_aus(context["active_memberships"])
        # Randspalte: nächste Sitzungen der Gremien der Person – nichts, was Kopf oder Liste schon zeigen
        context["naechste_sitzungen"] = list(
            OParlMeeting.objects.filter(
                organizations__in=[m.organization_id for m in context["active_memberships"]],
                start__gte=timezone.now(),
                cancelled=False,
                deleted=False,
            )
            .exclude(withdrawn_q())
            .prefetch_related("organizations")
            .distinct()
            .order_by("start")[:3]
        )
        context["hat_randspalte"] = bool(
            person.email or person.phone or person.title or person.gender or context["naechste_sitzungen"]
        )

        # Öffentliche Fragen (bei allen Mandatsträger:innen: Rat/Hauptorgan oder Fraktion). Pausiert
        # (Issue #734): Reiter nur mit bisherigen Fragen, ohne Antwortquote und ohne „Frage stellen“.
        from ..services import question_service

        enabled = question_service.questions_enabled()
        context["can_ask"] = enabled and (bool(council_membership) or question_service.is_mandate_holder(person))
        context["faction"] = question_service.get_faction(person)
        # Ohne Fraktion aus OParl: bestätigte Zuordnung aus Insight, mit Hinweis auf die Quelle (Issue #916)
        context["fraktion_lokal"] = None if context["faction"] else fraktionen.aktuelle_fraktion(person, person.body)
        if context["can_ask"] or not enabled:
            context["published_questions"] = PublicQuestion.objects.filter(
                recipient=person,
                status="published",
            ).order_by("-published_at", "-created_at")[:50]
        if context["can_ask"]:
            context["answer_stats"] = question_service.get_answer_stats(person)
        context["questions_tab"] = context["can_ask"] or bool(context.get("published_questions"))

        # SEO-Kontext
        from ..seo import get_person_seo
        from ..services.indexierung import robots_fuer_person

        # Ohne laufende Mitgliedschaft noindex (Issue #914); die Mitgliedschaften sind oben schon geladen
        context["seo"] = robots_fuer_person(
            get_person_seo(
                person,
                self.request,
                funktion=context["funktion"] or context["council_role"] or "",
                fraktion=context["faction"],
                mitgliedschaften=context["active_memberships"],
            ).to_dict(),
            context["active_memberships"],
        )

        return context
