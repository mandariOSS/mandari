# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q, Value
from django.db.models.functions import Coalesce, NullIf
from django.utils import timezone
from django.views.generic import DetailView, ListView

from hub.ris import selectors as ris_selectors

from ..models import (
    OParlOrganization,
    withdrawn_q,
)
from ..ranking import sort_organizations_by_ranking
from ..services import personen_liste, personen_sichtbarkeit, sitzverteilung
from ..services.testkonten import ausgeblendet_q
from ._helpers import ActiveBodyRequiredMixin, BegrenzteSeitenMixin, FilterInAdresseMixin, get_active_body
from ._withdrawn import withdrawn_response

# =============================================================================
# Gremien (Organizations)
# =============================================================================


class OrganizationListView(FilterInAdresseMixin, ActiveBodyRequiredMixin, BegrenzteSeitenMixin, ListView):
    """Liste aller Gremien mit Aktiv/Alle-Tabs."""

    model = OParlOrganization
    template_name = "pages/organizations/list.html"
    context_object_name = "organizations"
    paginate_by = 50
    teil_vorlage = "partials/organization_list_items.html"

    def get_queryset(self):
        body = get_active_body(self.request)
        if not body:
            return ris_selectors.organizations([]).none()

        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()
        today = timezone.now().date()
        now = timezone.now()

        # Aktiv = nicht abgelaufen UND hat mindestens eine Sitzung; „Alle“ auch aufgelöste Gremien. Suche in Name und
        # Kurzname. Nächste und letzte Sitzung als Unterabfrage über die Zuordnung Sitzung–Gremium (Indizes), laufende
        # Mitgliedschaften (Spalte „Mitglieder“ auf breiten Bildschirmen, Issue #841) ohne Test- und Systemkonten
        # (Issue #1258)
        base_qs = ris_selectors.public_organizations([body], q, on=today, include_ended=tab != "active").annotate(
            next_meeting=ris_selectors.next_meeting_start(now),
            last_meeting=ris_selectors.last_meeting_start(now),
            has_meetings=ris_selectors.has_public_meetings(),
            mitglieder=ris_selectors.current_member_count(today, exclude=ausgeblendet_q()),
            # Art des Gremiums: Klassifikation (Ausschuss, Beirat …), sonst der OParl-Typ
            art=Coalesce(NullIf("classification", Value("")), NullIf("organization_type", Value(""))),
        )
        if tab == "active":
            base_qs = base_qs.filter(has_meetings=True)

        return sort_organizations_by_ranking(base_qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        tab = self.request.GET.get("tab", "active")

        if body:
            today = timezone.now().date()

            # Counts ohne Suchfilter
            context["active_count"] = (
                ris_selectors.public_organizations([body], on=today)
                .annotate(has_meetings=ris_selectors.has_public_meetings())
                .filter(has_meetings=True)
                .count()
            )
            context["all_count"] = ris_selectors.public_organizations([body], include_ended=True).count()

        context["tab"] = tab
        from ..seo import get_list_seo

        # Kanonisch ist die Adresse je Kommune (Issue #939)
        context["seo"] = get_list_seo(
            self.request,
            "organization_list",
            title="Gremien & Ausschüsse",
            description=(
                f"Alle Gremien, Ausschüsse und Fraktionen in {body.get_display_name()} mit Mitgliedern und "
                "Sitzungen im Überblick."
                if body
                else "Alle Gremien, Ausschüsse und Fraktionen mit Mitgliedern und Sitzungen im Überblick."
            ),
            body=body,
        ).to_dict()
        return context


class OrganizationDetailView(DetailView):
    """Detailseite eines Gremiums."""

    model = OParlOrganization
    template_name = "pages/organizations/detail.html"
    context_object_name = "organization"

    def get(self, request, *args, **kwargs):
        # Von mandari Session zurückgenommen (gelöscht, Mandant deaktiviert): kein Inhalt
        self.object = self.get_object()
        if self.object.withdrawn_by_publisher:
            return withdrawn_response(request, self.object)
        return self.render_to_response(self.get_context_data(object=self.object))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object
        today = timezone.now().date()
        now = timezone.now()

        # Nur bestehende Mitgliedschaften von nicht zurückgenommenen Personen, ohne Test- und Systemkonten des RIS
        # (Issue #1258: sie zählen auch nicht als Mitglieder)
        # Genannt wird nur, wer derzeit ein Amt hat (Issue #1560): Ausgeschiedene ohne anderes Amt nicht
        all_memberships = personen_sichtbarkeit.mitgliedschaften_oeffentlicher(
            org.memberships.filter(deleted=False).exclude(withdrawn_q("person")).exclude(ausgeblendet_q())
        ).select_related("person", "person__body")
        active_qs = all_memberships.filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).order_by(
            "person__family_name"
        )
        past_qs = all_memberships.filter(end_date__lt=today).order_by("person__family_name")

        # Laufende Mitglieder mit ihrer Fraktion (Spalte „Fraktion“ aus OParl, Issue #841) und die Sitzverteilung als
        # Balken (Issue #1272, Fraktion wie in der Personenliste, auch gepflegt); auf Fraktionsseiten ohne beides
        active = list(active_qs)
        context["mitglieder_anzahl"] = len(active)
        context["sitzverteilung"] = None
        if not personen_liste.ist_fraktion(org):
            angaben = personen_liste.angaben_fuer({m.person_id: m.person for m in active}.values())
            for membership in active:
                membership.fraktion = angaben[membership.person_id].fraktion
            context["sitzverteilung"] = sitzverteilung.berechnen(
                active, {pid: a.fraktion or a.fraktion_lokal for pid, a in angaben.items()}
            )

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
            context["council_members"] = [m for m in active if m.role in council_roles]
            context["other_members"] = [m for m in active if m.role not in council_roles]
        else:
            context["active_members"] = active

        context["past_members"] = past_qs

        # Sitzungen
        context["upcoming_meetings"] = list(
            ris_selectors.public_meetings_of_organizations([org], starts_from=now, include_cancelled=False)[:10]
        )
        context["past_meetings"] = ris_selectors.public_meetings_of_organizations([org], starts_before=now).reverse()[
            :10
        ]

        # Randspalte (Issue #841): nur Angaben, die der Kopf nicht zeigt; ohne Inhalt keine Randspalte
        kurzname = org.short_name if org.short_name and org.short_name != org.name else ""
        context["kurzname"] = kurzname
        context["hat_angaben"] = bool(kurzname or org.start_date or org.end_date or org.website)
        context["hat_randspalte"] = context["hat_angaben"] or bool(context["upcoming_meetings"])
        # Feed des Gremiums (Issue #1274): kommende Sitzungen, Beschlüsse, bei Fraktionen ihre Vorgänge
        from .feeds import abo

        context["abo_feed"] = abo(
            "organization_feed", f"Neues aus {org.name or 'diesem Gremium'}", kwargs={"pk": org.pk}
        )

        # SEO-Kontext
        from ..seo import get_organization_seo

        context["seo"] = get_organization_seo(
            org,
            self.request,
            mitglieder=context["mitglieder_anzahl"],
            naechste_sitzung=next(iter(context["upcoming_meetings"]), None),
        ).to_dict()

        return context
