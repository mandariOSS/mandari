# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Digitale Abstimmung und Umlaufbeschlüsse (Issue #41).

- Einzelstimmen-Erfassung je TOP für die Protokollführung
  (offen/namentlich/geheim, Befangenheit nach Gemeindeordnung)
- Umlaufbeschlüsse: Anlage, Rücklauf-Erfassung, Ergebnisfeststellung
"""

from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import (
    SessionAgendaItem,
    SessionCircularResolution,
    SessionCircularVote,
    SessionOrganization,
    SessionPaper,
    SessionVote,
)
from ..permissions import SessionViewMixin
from ..services import voting_service
from .nexturl import safe_next_url


def _get_item(view, item_id):
    qs = SessionAgendaItem.objects.filter(meeting__tenant=view.session_tenant).select_related(
        "meeting__organization", "meeting__tenant"
    )
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True, meeting__is_public=True)
    return get_object_or_404(qs, pk=item_id)


class VotingCaptureView(SessionViewMixin, TemplateView):
    """
    Abstimmung zu einem TOP erfassen: Abstimmungsart + Einzelstimmen aus
    der Anwesenheitsliste (Schnellerfassung für die Protokollführung).
    """

    template_name = "session/voting/capture.html"
    permission_required = "edit_protocols"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        item = _get_item(self, self.kwargs["item_id"])
        attendances = list(
            item.meeting.attendances.select_related("person").order_by("person__family_name", "person__given_name")
        )
        votes = {v.person_id: v.vote for v in item.votes.all()}
        for attendance in attendances:
            attendance.current_vote = votes.get(attendance.person_id, "")
        context.update(
            {
                "item": item,
                "meeting": item.meeting,
                "attendances": attendances,
                "method_choices": SessionAgendaItem.VOTING_METHOD_CHOICES,
                "vote_choices": SessionVote.VOTE_CHOICES,
                "result_choices": SessionAgendaItem._meta.get_field("vote_result").choices,
                "tally": voting_service.tally(item),
            }
        )
        return context

    def post(self, request, tenant_slug, item_id):
        item = _get_item(self, item_id)

        method = request.POST.get("voting_method", item.voting_method)
        if method not in {value for value, _ in SessionAgendaItem.VOTING_METHOD_CHOICES}:
            method = item.voting_method
        item.voting_method = method

        # Ergebnis (optional mitpflegen)
        result = request.POST.get("vote_result", "")
        if result in {value for value, _ in item._meta.get_field("vote_result").choices}:
            item.vote_result = result

        if method == "secret":
            # Geheim: Summen manuell, keine Einzelstimmen
            for field in ("votes_yes", "votes_no", "votes_abstain"):
                try:
                    setattr(item, field, max(0, min(9999, int(request.POST.get(field, 0)))))
                except (TypeError, ValueError):
                    pass
        item.save()

        votes_by_person = {}
        for attendance in item.meeting.attendances.select_related("person"):
            key = f"vote_{attendance.person_id}"
            if key in request.POST:
                votes_by_person[attendance.person] = request.POST.get(key, "")
        tally = voting_service.capture_votes(item, votes_by_person, recorded_by=self.session_user)

        audit.log_event(
            "update",
            item,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "abstimmung": item.get_voting_method_display(),
                "ergebnis": item.get_vote_result_display(),
                "ja": item.votes_yes,
                "nein": item.votes_no,
                "enthaltung": item.votes_abstain,
                "befangen": [p.display_name for p in tally["excluded"]],
            },
        )
        messages.success(
            request,
            f"Abstimmung zu TOP {item.number} erfasst "
            f"(Ja {item.votes_yes} / Nein {item.votes_no} / Enthaltung {item.votes_abstain}).",
        )
        next_url = safe_next_url(request, self.session_tenant.slug)
        if next_url:
            return redirect(next_url)
        return redirect("session:voting_capture", tenant_slug=tenant_slug, item_id=item.id)


class CircularListView(SessionViewMixin, TemplateView):
    """Liste der Umlaufbeschlüsse."""

    template_name = "session/voting/circular_list.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = SessionCircularResolution.objects.filter(tenant=self.session_tenant).select_related("organization")
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        context.update(
            {
                "circulars": qs[:200],
                "can_manage": self.has_permission("edit_meetings"),
                "organizations": SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True)
                .exclude(organization_type="department")
                .order_by("name"),
                "papers": SessionPaper.objects.filter(tenant=self.session_tenant, status="approved").order_by(
                    "-created_at"
                )[:100],
                "today": timezone.localdate(),
            }
        )
        return context


class CircularCreateView(SessionViewMixin, View):
    """Umlaufbeschluss anlegen (startet den Umlauf)."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        organization = None
        org_id = request.POST.get("organization", "").strip()
        if org_id:
            try:
                organization = (
                    SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True, pk=org_id)
                    .exclude(organization_type="department")
                    .first()
                )
            except (ValueError, DjangoValidationError):
                organization = None

        title = request.POST.get("title", "").strip()[:500]
        resolution_text = request.POST.get("resolution_text", "").strip()
        try:
            deadline = date.fromisoformat(request.POST.get("deadline", ""))
        except (TypeError, ValueError):
            deadline = None

        if organization is None or not title or not resolution_text or deadline is None:
            messages.error(
                request,
                "Bitte Gremium, Betreff, Beschlussvorschlag und Rückmeldefrist angeben.",
            )
            return redirect("session:circulars", tenant_slug=tenant_slug)

        paper = None
        paper_id = request.POST.get("paper", "").strip()
        if paper_id:
            try:
                paper = SessionPaper.objects.filter(tenant=self.session_tenant, pk=paper_id).first()
            except (ValueError, DjangoValidationError):
                paper = None

        circular = SessionCircularResolution.objects.create(
            tenant=self.session_tenant,
            organization=organization,
            title=title,
            resolution_text=resolution_text,
            paper=paper,
            deadline=deadline,
            is_public=request.POST.get("is_public", "1") == "1",
            created_by=self.session_user,
        )
        voting_service.assign_circular_number(circular)
        audit.log_event(
            "create",
            circular,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"umlauf": circular.reference, "gremium": organization.name, "frist": deadline.isoformat()},
        )
        messages.success(request, f"Umlaufbeschluss {circular.reference} wurde gestartet.")
        return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)


class CircularDetailView(SessionViewMixin, TemplateView):
    """Umlaufbeschluss: Rückläufe erfassen, Ergebnis feststellen."""

    template_name = "session/voting/circular_detail.html"
    permission_required = "view_meetings"

    def _get_circular(self):
        qs = SessionCircularResolution.objects.filter(tenant=self.session_tenant).select_related(
            "organization", "paper", "created_by__user"
        )
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        return get_object_or_404(qs, pk=self.kwargs["circular_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        circular = self._get_circular()
        tally = voting_service.circular_tally(circular)
        votes = {v.person_id: v for v in circular.votes.select_related("person")}
        members = list(voting_service.voting_members(circular))
        for membership in members:
            membership.vote = votes.get(membership.person_id)
        context.update(
            {
                "circular": circular,
                "tally": tally,
                "members": members,
                "can_manage": self.has_permission("edit_meetings"),
                "vote_choices": SessionCircularVote.VOTE_CHOICES,
                "today": timezone.localdate(),
            }
        )
        return context


class CircularVoteView(SessionViewMixin, View):
    """Rücklauf eines Mitglieds erfassen bzw. korrigieren."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, circular_id):
        circular = get_object_or_404(
            SessionCircularResolution.objects.filter(tenant=self.session_tenant),
            pk=circular_id,
        )
        if circular.status != "open":
            messages.error(request, "Der Umlauf ist bereits abgeschlossen.")
            return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)

        membership = (
            voting_service.voting_members(circular).filter(person_id=request.POST.get("person")).first()
            if request.POST.get("person")
            else None
        )
        vote_value = request.POST.get("vote", "")
        if membership is None or vote_value not in {v for v, _ in SessionCircularVote.VOTE_CHOICES}:
            messages.error(request, "Bitte Person und Stimme angeben.")
            return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)

        try:
            received_at = date.fromisoformat(request.POST.get("received_at", ""))
        except (TypeError, ValueError):
            received_at = timezone.localdate()

        SessionCircularVote.objects.update_or_create(
            circular=circular,
            person=membership.person,
            defaults={
                "vote": vote_value,
                "received_at": received_at,
                "recorded_by": self.session_user,
            },
        )
        audit.log_event(
            "update",
            circular,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "umlauf": circular.reference,
                "ruecklauf": membership.person.display_name,
                "stimme": dict(SessionCircularVote.VOTE_CHOICES)[vote_value],
            },
        )
        messages.success(request, f"Rücklauf von {membership.person.display_name} erfasst.")
        return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)


class CircularCloseView(SessionViewMixin, View):
    """Umlauf abschließen: Ergebnis feststellen oder abbrechen."""

    permission_required = "edit_meetings"
    http_method_names = ["post"]

    RESULTS = {"adopted", "rejected", "cancelled"}

    def post(self, request, tenant_slug, circular_id):
        circular = get_object_or_404(
            SessionCircularResolution.objects.filter(tenant=self.session_tenant),
            pk=circular_id,
        )
        result = request.POST.get("result", "")
        if circular.status != "open" or result not in self.RESULTS:
            messages.error(request, "Der Umlauf ist bereits abgeschlossen.")
            return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)

        tally = voting_service.circular_tally(circular)
        circular.status = result
        circular.result_note = request.POST.get("result_note", "").strip()
        circular.decided_at = timezone.now()
        circular.save()

        audit.log_event(
            "update",
            circular,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "umlauf": circular.reference,
                "ergebnis": circular.get_status_display(),
                "ja": tally["yes"],
                "nein": tally["no"],
                "enthaltung": tally["abstain"],
                "ruecklaeufe": f"{tally['responded']}/{tally['total_members']}",
            },
        )
        messages.success(
            request,
            f"Umlaufbeschluss {circular.reference}: {circular.get_status_display()} "
            f"(Ja {tally['yes']} / Nein {tally['no']} / Enthaltung {tally['abstain']}).",
        )
        return redirect("session:circular_detail", tenant_slug=tenant_slug, circular_id=circular.id)
