# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from ..models import (
    OParlBody,
)
from ._helpers import get_active_body

# =============================================================================
# Öffentliche Fraktionsprotokolle
# =============================================================================


class PublicProtocolListView(TemplateView):
    """
    Öffentliches Dashboard für genehmigte Fraktionsprotokolle.

    Zeigt nur Protokolle die:
    - Status 'approved' haben
    - Zu einer Organisation gehören die öffentliche Protokolle erlaubt

    Keine Authentifizierung erforderlich.
    """

    template_name = "pages/public/protocols/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get body from URL slug
        body_slug = kwargs.get("body_slug")
        body = get_object_or_404(OParlBody, slug=body_slug) if body_slug else get_active_body(self.request)

        if not body:
            context["protocols"] = []
            context["body"] = None
            return context

        context["body"] = body

        # Find organizations linked to this body that allow public protocols
        # (expliziter Opt-in je Organisation, Default: aus)
        from apps.tenants.models import Organization

        organizations = Organization.objects.filter(
            body=body,
            is_active=True,
            publish_protocols=True,
        )

        # Get approved protocols from these organizations
        from apps.work.faction.models import FactionMeeting

        protocols = (
            FactionMeeting.objects.filter(
                organization__in=organizations,
                protocol_status="approved",
                status="completed",
            )
            .select_related("organization")
            .order_by("-start")
        )

        # Filter and search
        search = self.request.GET.get("q", "").strip()
        if search:
            protocols = protocols.filter(Q(title__icontains=search) | Q(description__icontains=search))

        # Year filter
        year = self.request.GET.get("year")
        if year:
            protocols = protocols.filter(start__year=year)

        # Pagination
        paginator = Paginator(protocols, 20)
        page = self.request.GET.get("page", 1)
        context["protocols"] = paginator.get_page(page)

        # Available years for filter
        years = FactionMeeting.objects.filter(
            organization__in=organizations,
            protocol_status="approved",
        ).dates("start", "year", order="DESC")
        context["available_years"] = [d.year for d in years]

        context["search_query"] = search
        context["selected_year"] = year

        return context


class PublicProtocolDetailView(TemplateView):
    """
    Öffentliche Detailansicht eines genehmigten Fraktionsprotokolls.

    Zeigt nur öffentliche TOPs (visibility='public').
    Keine Authentifizierung erforderlich.
    """

    template_name = "pages/public/protocols/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        from apps.work.faction.models import FactionMeeting

        # Get the meeting — nur genehmigte Protokolle von Organisationen,
        # die die Veröffentlichung explizit eingeschaltet haben
        meeting_id = kwargs.get("meeting_id")
        meeting = get_object_or_404(
            FactionMeeting,
            id=meeting_id,
            protocol_status="approved",  # Only approved protocols
            status="completed",
            organization__is_active=True,
            organization__publish_protocols=True,
        )

        context["meeting"] = meeting
        context["organization"] = meeting.organization
        context["body"] = meeting.organization.get_primary_body() if meeting.organization else None

        # Only show public agenda items
        agenda_items = meeting.agenda_items.filter(
            visibility="public",
            proposal_status="active",  # Only accepted items
        ).order_by("order", "number")

        context["agenda_items"] = agenda_items

        # Get public protocol entries — AUSSCHLIESSLICH Einträge, die einem
        # öffentlichen TOP zugeordnet sind. TOP-lose Einträge (agenda_item=None)
        # können interne Notizen sein und werden nie öffentlich ausgegeben.
        public_item_ids = agenda_items.values_list("id", flat=True)
        protocol_entries = (
            meeting.protocol_entries.filter(agenda_item_id__in=public_item_ids)
            .select_related("agenda_item", "speaker__user")
            .order_by("order", "created_at")
        )

        context["protocol_entries"] = protocol_entries

        # Previous/Next navigation
        context["previous_meeting"] = (
            FactionMeeting.objects.filter(
                organization=meeting.organization,
                protocol_status="approved",
                start__lt=meeting.start,
            )
            .order_by("-start")
            .first()
        )

        context["next_meeting"] = (
            FactionMeeting.objects.filter(
                organization=meeting.organization,
                protocol_status="approved",
                start__gt=meeting.start,
            )
            .order_by("start")
            .first()
        )

        return context
