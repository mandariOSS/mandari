# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Besetzungs-Verwaltung (Gremienmitgliedschaften) für das Session RIS (Issue #27).

Mitgliedschaften mit Funktion (Vorsitz, stellv. Vorsitz, Mitglied,
sachkundige/r Bürger/in, …), Stimmrecht, Vertreterregelung und Zeitraum —
inklusive Nachrücker-Flow (Mitgliedschaft beenden + Nachfolger anlegen
in einem Schritt). Alle Änderungen werden über die Audit-Signale
protokolliert.

Plausibilität (``membership_service``): Das Ende liegt nie vor dem Beginn, eine Person hat in einem
Gremium keine zwei Besetzungen mit überschneidendem Zeitraum, und die Wahlperiode folgt dem Beginn.
Ungültige Eingaben ergeben eine Meldung, nie einen Serverfehler oder einen halben Stand.
"""

from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View

from apps.common.formatting import parse_iso_date
from apps.common.params import uuid_param

from ..models import (
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import membership_service

# =============================================================================
# HELPERS
# =============================================================================


def _get_membership(view, membership_id):
    return get_object_or_404(
        SessionOrganizationMembership.objects.select_related("organization", "person"),
        pk=membership_id,
        organization__tenant=view.session_tenant,
    )


def _org_redirect(view, organization):
    return redirect(
        "session:organization_detail",
        tenant_slug=view.session_tenant.slug,
        organization_id=organization.id,
    )


def _tenant_person(view, raw_id) -> SessionPerson | None:
    """Person des Mandanten zu einer Kennung aus dem Formular; ungültig oder fremd ergibt None."""
    person_id = uuid_param(raw_id)
    if person_id is None:
        return None
    return SessionPerson.objects.filter(pk=person_id, tenant=view.session_tenant).first()


def _valid_role(raw_role, default: str) -> str:
    valid_roles = {c[0] for c in SessionOrganizationMembership._meta.get_field("role").choices}
    return raw_role if raw_role in valid_roles else default


def _recall_applies(organization, role: str) -> bool:
    """Abberufung mit Sperrvermerk gibt es nur für den Vorsitz eines Ausschusses (§ 71 Abs. 8 NKomVG)."""
    return role == "chair" and organization.organization_type == "committee"


# =============================================================================
# VIEWS
# =============================================================================


class MembershipCreateView(SessionViewMixin, View):
    """Besetzung anlegen (aus der Gremien-Detailseite)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, organization_id):
        organization = get_object_or_404(SessionOrganization, pk=organization_id, tenant=self.session_tenant)
        person = _tenant_person(self, request.POST.get("person"))
        if person is None:
            messages.error(request, "Bitte eine Person auswählen.")
            return _org_redirect(self, organization)
        substitute_for = None
        if request.POST.get("substitute_for"):
            substitute_for = _tenant_person(self, request.POST["substitute_for"])
            if substitute_for is None:
                messages.error(request, "Die vertretene Person wurde nicht gefunden.")
                return _org_redirect(self, organization)

        start_date = parse_iso_date(request.POST.get("start_date")) or timezone.localdate()
        end_date = parse_iso_date(request.POST.get("end_date"))
        error = membership_service.period_error(start_date, end_date) or membership_service.overlap_error(
            organization, person, start_date, end_date
        )
        role = _valid_role(request.POST.get("role", "member"), "member")
        # Funktion nach Landesrecht (Issue #757): Altersgrenze, Sperrvermerk nach Abberufung
        error = error or " ".join(membership_service.role_problems(organization, person, role, start_date))
        if error:
            messages.error(request, error)
            return _org_redirect(self, organization)

        membership = SessionOrganizationMembership.objects.create(
            organization=organization,
            person=person,
            role=role,
            has_voting_rights=membership_service.voting_rights(role, request.POST.get("has_voting_rights") == "on"),
            substitute_for=substitute_for,
            start_date=start_date,
            end_date=end_date,
            # Wahlperiode aus dem Beginn ableiten (Issue #39) – außerhalb jeder Periode keine
            legislative_term=membership_service.term_for(self.session_tenant, start_date),
        )
        messages.success(
            request,
            f"{person.display_name} wurde als {membership.get_role_display()} aufgenommen.",
        )
        return _org_redirect(self, organization)


class MembershipUpdateView(SessionViewMixin, View):
    """Besetzung bearbeiten (Funktion, Stimmrecht, Vertretung, Zeitraum)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)
        organization = membership.organization

        start_date = membership.start_date
        if "start_date" in request.POST:
            start_date = parse_iso_date(request.POST.get("start_date")) or membership.start_date
        end_date = membership.end_date
        if "end_date" in request.POST:
            end_date = parse_iso_date(request.POST.get("end_date"))
        error = membership_service.period_error(start_date, end_date) or membership_service.overlap_error(
            organization, membership.person, start_date, end_date, exclude_pk=membership.pk
        )
        role = _valid_role(request.POST.get("role", membership.role), membership.role)
        if not error and (role != membership.role or start_date != membership.start_date):
            # Funktion nach Landesrecht (Issue #757): Altersgrenze, Sperrvermerk nach Abberufung
            error = " ".join(
                membership_service.role_problems(
                    organization, membership.person, role, start_date, exclude_pk=membership.pk
                )
            )
        # Sperrvermerk der Abberufung (Issue #757, § 71 Abs. 8): nur am Vorsitz eines Ausschusses, mit Ende; ein
        # versehentlich gesetzter Vermerk lässt sich hier zurücknehmen (Prüfprotokoll über das Speichersignal)
        recall_field = "end_reason_shown" in request.POST and _recall_applies(organization, role)
        recalled = request.POST.get("end_reason") == SessionOrganizationMembership.END_RECALLED
        if not error and recall_field and recalled and end_date is None:
            error = "Für die Abberufung bitte das Ende der Besetzung angeben."
        if error:
            messages.error(request, error)
            return _org_redirect(self, organization)

        if recall_field:
            membership.end_reason = SessionOrganizationMembership.END_RECALLED if recalled else ""
        if "substitute_for" in request.POST:
            if request.POST["substitute_for"]:
                substitute_for = _tenant_person(self, request.POST["substitute_for"])
                if substitute_for is None:
                    messages.error(request, "Die vertretene Person wurde nicht gefunden.")
                    return _org_redirect(self, organization)
                membership.substitute_for = substitute_for
            else:
                membership.substitute_for = None

        membership.role = role
        membership.has_voting_rights = membership_service.voting_rights(
            role, request.POST.get("has_voting_rights") == "on"
        )
        if start_date != membership.start_date:
            # Wahlperiode folgt dem Beginn (Issue #39)
            membership.legislative_term = membership_service.term_for(self.session_tenant, start_date)
        membership.start_date = start_date
        membership.end_date = end_date
        membership.save()
        messages.success(request, f"Besetzung von {membership.person.display_name} wurde aktualisiert.")
        return _org_redirect(self, organization)


class MembershipEndView(SessionViewMixin, View):
    """Mitgliedschaft beenden (Ausscheiden); das Ende ist der letzte Tag der Mitgliedschaft."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)
        end_date = parse_iso_date(request.POST.get("end_date")) or timezone.localdate()
        error = membership_service.period_error(membership.start_date, end_date)
        if error:
            messages.error(request, error)
            return _org_redirect(self, membership.organization)
        membership.end_date = end_date
        # Abberufung eines Ausschussvorsitzes (Issue #757, § 71 Abs. 8 NKomVG): Sperrvermerk für eine erneute
        # Benennung. Ein späteres Verschieben des Endes ohne Häkchen hebt den Vermerk nicht auf; zurücknehmen lässt
        # er sich in der Bearbeitung der Besetzung.
        recalled = request.POST.get("end_reason") == SessionOrganizationMembership.END_RECALLED and _recall_applies(
            membership.organization, membership.role
        )
        if recalled:
            membership.end_reason = SessionOrganizationMembership.END_RECALLED
        membership.save()
        messages.success(
            request,
            f"Mitgliedschaft von {membership.person.display_name} wurde zum {membership.end_date:%d.%m.%Y} "
            f"{'durch Abberufung ' if recalled else ''}beendet.",
        )
        return _org_redirect(self, membership.organization)


class MembershipSuccessionView(SessionViewMixin, View):
    """
    Nachrücker-Flow: Mitgliedschaft beenden + Nachfolger anlegen in einem Schritt.

    Die ausscheidende Person ist bis zum Vortag Mitglied, die nachrückende ab dem gewählten Tag – so hat
    das Gremium an keinem Tag einen Sitz doppelt (Ladung, Anwesenheit, Beschlussfähigkeit). Beides gilt
    nur zusammen (eine Transaktion). Ist das Ausscheiden zum Vortag schon erfasst, bleibt es unverändert.
    """

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)
        organization = membership.organization

        successor = _tenant_person(self, request.POST.get("successor"))
        if successor is None:
            messages.error(request, "Bitte die nachrückende Person auswählen.")
            return _org_redirect(self, organization)
        if successor.pk == membership.person_id:
            messages.error(request, "Nachrücker/in darf nicht die ausscheidende Person sein.")
            return _org_redirect(self, organization)

        change_date = parse_iso_date(request.POST.get("change_date")) or timezone.localdate()
        last_day = change_date - timedelta(days=1)
        if membership.start_date is not None and last_day < membership.start_date:
            messages.error(
                request,
                f"Der Wechsel muss nach dem Beginn der Mitgliedschaft ({membership.start_date:%d.%m.%Y}) liegen.",
            )
            return _org_redirect(self, organization)
        if membership.end_date is not None and membership.end_date < last_day:
            messages.error(
                request,
                f"Die Mitgliedschaft endete bereits am {membership.end_date:%d.%m.%Y}; bitte die Person direkt aufnehmen.",
            )
            return _org_redirect(self, organization)
        # Ausscheiden schon erfasst (Ende genau am Vortag des Wechsels): Das Ende war der Austritt, kein
        # geplantes Periodenende – die nachrückende Person erhält daher kein Ende. Sonst übernimmt sie ein
        # geplantes Ende (z. B. das Ende der Wahlperiode), das dann nie vor dem Wechsel liegt.
        already_ended = membership.end_date == last_day
        successor_end = None if already_ended else membership.end_date
        error = membership_service.period_error(change_date, successor_end) or membership_service.overlap_error(
            organization, successor, change_date, successor_end
        )
        if error:
            messages.error(request, error)
            return _org_redirect(self, organization)

        with transaction.atomic():
            # 1) Ausscheiden dokumentieren: letzter Tag ist der Vortag des Wechsels
            if not already_ended:
                membership.end_date = last_day
                membership.save()

            # 2) Nachfolger mit gleicher Funktion/gleichem Stimmrecht anlegen
            SessionOrganizationMembership.objects.create(
                organization=organization,
                person=successor,
                role=membership.role,
                has_voting_rights=membership.has_voting_rights,
                start_date=change_date,
                end_date=successor_end,
                # Wahlperiode aus dem Stichtag ableiten (Issue #39)
                legislative_term=membership_service.term_for(self.session_tenant, change_date),
            )

        messages.success(
            request,
            f"{successor.display_name} rückt zum {change_date:%d.%m.%Y} für {membership.person.display_name} nach "
            f"({membership.person.display_name} gehört dem Gremium bis {last_day:%d.%m.%Y} an).",
        )
        return _org_redirect(self, organization)
