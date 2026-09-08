# SPDX-License-Identifier: AGPL-3.0-or-later
"""Öffentliches Beschluss-Tracking „Was wurde aus …?“ (Issue #48)."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Q
from django.forms import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import DecisionSubscription
from ..services import decision_tracking
from ._helpers import ActiveBodyRequiredMixin, get_active_body

PAGE_SIZE = 25


class DecisionListView(ActiveBodyRequiredMixin, TemplateView):
    template_name = "pages/decisions/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        context["active_body"] = body
        context["nav"] = "decision_list"
        tenants = decision_tracking.publishing_tenants(body)
        context["publishing"] = tenants.exists()
        if not context["publishing"]:
            return context

        params = self.request.GET
        qs = decision_tracking.public_decisions(body)
        status = params.get("status", "")
        organization_id = params.get("gremium", "")
        year = params.get("jahr", "")
        query = (params.get("q") or "").strip()
        only_open = params.get("offen") == "1"

        if status in decision_tracking.PUBLIC_STATUS_LABELS:
            qs = qs.filter(implementation_status=status)
        if only_open:
            qs = qs.exclude(implementation_status="done")
        if organization_id:
            qs = qs.filter(meeting__organization_id=organization_id)
        if year.isdigit():
            qs = qs.filter(meeting__start__year=int(year))
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(resolution_text__icontains=query)
                | Q(resolution_number__icontains=query)
                | Q(implementation_public_note__icontains=query)
            )

        from apps.session.models import SessionOrganization

        page = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        items = list(page.object_list)
        for item in items:
            item.public_label = decision_tracking.PUBLIC_STATUS_LABELS.get(
                item.implementation_status, item.get_implementation_status_display()
            )
        context.update(
            {
                "stats": decision_tracking.stats(body),
                "status_labels": decision_tracking.PUBLIC_STATUS_LABELS,
                "items": items,
                "page_obj": page,
                "organizations": SessionOrganization.objects.filter(
                    tenant__in=tenants, is_active=True, organization_type__in=["council", "committee", "advisory"]
                ).order_by("name"),
                "years": sorted(
                    {
                        d.year
                        for d in decision_tracking.public_decisions(body).values_list("meeting__start", flat=True)
                        if d
                    },
                    reverse=True,
                ),
                "filter_status": status,
                "filter_organization": organization_id,
                "filter_year": year,
                "filter_open": only_open,
                "search_query": query,
                "has_filter": bool(status or organization_id or year or query or only_open),
                "today": timezone.localdate(),
            }
        )
        return context


class DecisionDetailView(TemplateView):
    template_name = "pages/decisions/detail.html"

    def _get_item(self):
        from apps.session.models import SessionAgendaItem

        item = get_object_or_404(
            SessionAgendaItem.objects.select_related("meeting__organization", "meeting__tenant__oparl_body", "paper"),
            id=self.kwargs["pk"],
        )
        if not decision_tracking.is_publicly_visible(item):
            raise Http404("Beschluss nicht öffentlich")
        return item

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        item = kwargs.get("item") or self._get_item()
        body = item.meeting.tenant.oparl_body
        # Kommune der Seite folgt dem Beschluss (Deep-Links aus E-Mails)
        if str(self.request.session.get("active_body_id")) != str(body.id):
            self.request.session["active_body_id"] = str(body.id)
        context.update(
            {
                "active_body": body,
                "nav": "decision_detail",
                "item": item,
                "tenant": item.meeting.tenant,
                "timeline": decision_tracking.timeline(item),
                "status_label": decision_tracking.PUBLIC_STATUS_LABELS.get(
                    item.implementation_status, item.get_implementation_status_display()
                ),
                "overdue": bool(
                    item.implementation_deadline
                    and item.implementation_status != "done"
                    and item.implementation_deadline < timezone.localdate()
                ),
                "oparl_meeting_id": item.meeting.oparl_meeting_id,
                "oparl_paper_id": item.paper.oparl_paper_id if item.paper_id else None,
                "subscriber_count": DecisionSubscription.objects.filter(
                    agenda_item=item, confirmed=True, unsubscribed_at__isnull=True
                ).count(),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        """Beschluss abonnieren (Double-Opt-In)."""
        item = self._get_item()
        email = (request.POST.get("email") or "").strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Bitte eine gültige E-Mail-Adresse angeben.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        if request.POST.get("privacy") != "on":
            messages.error(request, "Bitte der Verarbeitung der E-Mail-Adresse zustimmen.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)

        subscription, created = DecisionSubscription.objects.get_or_create(agenda_item=item, email=email)
        if not created and subscription.confirmed and subscription.unsubscribed_at is None:
            messages.info(request, "Diese Adresse ist für den Beschluss bereits eingetragen.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        if not created:
            subscription.unsubscribed_at = None
            subscription.confirmed = False
            subscription.save(update_fields=["unsubscribed_at", "confirmed"])
        decision_tracking.send_confirmation(subscription)
        messages.success(
            request,
            "Fast geschafft: Wir haben eine E-Mail zur Bestätigung geschickt. Erst nach dem Klick auf den Link "
            "erhalten Sie Benachrichtigungen zu diesem Beschluss.",
        )
        return redirect("insight_core:insight:decision_detail", pk=item.id)


@require_GET
def confirm_decision_subscription(request, token):
    subscription = get_object_or_404(DecisionSubscription.objects.select_related("agenda_item__meeting"), token=token)
    already = subscription.confirmed and subscription.unsubscribed_at is None
    if not already:
        subscription.confirmed = True
        subscription.confirmed_at = timezone.now()
        subscription.unsubscribed_at = None
        subscription.save(update_fields=["confirmed", "confirmed_at", "unsubscribed_at"])
    return render(
        request,
        "pages/decisions/subscription_status.html",
        {
            "item": subscription.agenda_item,
            "title": "Benachrichtigung aktiv" if not already else "Bereits bestätigt",
            "message": (
                "Sie erhalten eine E-Mail, sobald die Verwaltung einen neuen Umsetzungsstand zu diesem Beschluss meldet."
                if not already
                else "Diese Benachrichtigung war bereits aktiv."
            ),
            "unsubscribe_token": subscription.token,
        },
    )


@require_GET
def unsubscribe_decision(request, token):
    subscription = get_object_or_404(DecisionSubscription.objects.select_related("agenda_item__meeting"), token=token)
    if subscription.unsubscribed_at is None:
        subscription.unsubscribed_at = timezone.now()
        subscription.save(update_fields=["unsubscribed_at"])
    return render(
        request,
        "pages/decisions/subscription_status.html",
        {
            "item": subscription.agenda_item,
            "title": "Benachrichtigung beendet",
            "message": "Sie erhalten keine weiteren E-Mails zu diesem Beschluss. Die Adresse wird nicht weiter verwendet.",
            "unsubscribe_token": None,
        },
    )
