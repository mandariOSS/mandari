# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.

Simplified architecture: 4 views instead of 13.
- FactionMeetingListView: List + Create (POST)
- FactionMeetingDetailView: Detail/Protocol page
- FactionActionView: Central HTMX action handler
- FactionSettingsView: Legacy redirect to organization settings
"""

import logging
from datetime import timedelta

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import (
    FactionAttendance,
    FactionMeeting,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_meeting_context(view, meeting):
    """Build shared context dict for detail page and partials."""
    from apps.common.permissions import PermissionChecker

    from ..visibility import LOCKED_PLACEHOLDER
    from ..visibility import can_view_internal as _can_view_internal

    checker = PermissionChecker(view.membership)

    # Agenda items (top-level only, children via prefetch)
    agenda_items = (
        meeting.agenda_items.filter(parent__isnull=True)
        .select_related("related_agenda_item", "approves_meeting")
        .prefetch_related("protocol_entries", "protocol_entries__speaker__user", "children")
        .order_by("order", "number")
    )

    public_items = [i for i in agenda_items if i.visibility == "public"]
    can_view_internal = _can_view_internal(view.membership)
    # NÖ strikt (Issue #64): Nicht-Vereidigte erhalten KEINE NÖ-Objekte im
    # Kontext — nur die Anzahl für "Gesperrte Information"-Platzhalter
    internal_items = [i for i in agenda_items if i.visibility == "internal"] if can_view_internal else []
    locked_internal_count = 0 if can_view_internal else sum(1 for i in agenda_items if i.visibility == "internal")

    # Attendance
    attendances = meeting.attendances.select_related("membership__user")

    try:
        my_attendance = meeting.attendances.get(membership=view.membership)
    except FactionAttendance.DoesNotExist:
        my_attendance = None

    attendance_stats = {
        "confirmed": sum(1 for a in attendances if a.status == "confirmed"),
        "declined": sum(1 for a in attendances if a.status == "declined"),
        "tentative": sum(1 for a in attendances if a.status == "tentative"),
        "pending": sum(1 for a in attendances if a.status == "invited"),
        "present": sum(1 for a in attendances if a.status == "present"),
        "absent": sum(1 for a in attendances if a.status == "absent"),
        "excused": sum(1 for a in attendances if a.status == "excused"),
    }

    # Permissions
    can_edit = (
        meeting.created_by == view.membership or view.membership.has_permission("faction.manage")
    ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

    # Protokollphase: während der Sitzung und nach Sitzungsende bis zur
    # Protokoll-Genehmigung dürfen Berechtigte protokollieren/abstimmen.
    is_protocol_phase = meeting.status in ["ongoing", "completed"] and not meeting.protocol_approved
    can_protocol = is_protocol_phase and (
        meeting.created_by == view.membership
        or view.membership.has_permission("faction.manage")
        or view.membership.has_permission("protocols.create")
        or view.membership.has_permission("protocols.edit")
    )

    start_allowed_from = meeting.start - timedelta(minutes=30)
    can_start = (
        view.membership.has_permission("faction.start")
        and meeting.status in ["planned", "invited"]
        and start_allowed_from <= timezone.now()
    )

    can_manage_attendance = view.membership.has_permission("faction.manage")
    can_propose = checker.can_propose_agenda_items()
    can_create_directly = checker.can_create_agenda_items_directly()

    # Available members (for adding attendees)
    existing_ids = list(attendances.filter(membership__isnull=False).values_list("membership_id", flat=True))
    from apps.tenants.models import Membership

    available_members = (
        Membership.objects.filter(organization=view.organization, is_active=True)
        .exclude(id__in=existing_ids)
        .select_related("user")
        .order_by("user__last_name", "user__first_name")
    )

    # Protocol entries (sidebar summary) — NÖ strikt (Issue #64):
    # Einträge zu NÖ-TOPs erscheinen für Nicht-Vereidigte nirgends
    protocol_entries_qs = meeting.protocol_entries.select_related(
        "agenda_item", "speaker__user", "created_by__user"
    ).order_by("-created_at")
    if not can_view_internal:
        protocol_entries_qs = protocol_entries_qs.exclude(agenda_item__visibility="internal")
    protocol_entries = protocol_entries_qs[:10]

    protocol_entry_count = protocol_entries_qs.count()

    # TOP-Vorschläge: NÖ-Vorschläge sind für Nicht-Vereidigte unsichtbar
    pending_proposals = meeting.agenda_items.filter(proposal_status="proposed")
    if not can_view_internal:
        pending_proposals = pending_proposals.exclude(visibility="internal")

    return {
        "meeting": meeting,
        # NÖ strikt: Nicht-Vereidigte bekommen keine NÖ-Objekte in den Kontext
        "agenda_items": list(agenda_items) if can_view_internal else public_items,
        "public_agenda_items": public_items,
        "internal_agenda_items": internal_items,
        "can_view_internal": can_view_internal,
        "locked_internal_count": locked_internal_count,
        "locked_placeholder": LOCKED_PLACEHOLDER,
        "attendances": attendances,
        "my_attendance": my_attendance,
        "attendance_stats": attendance_stats,
        "can_edit": can_edit,
        "is_protocol_phase": is_protocol_phase,
        "can_protocol": can_protocol,
        "can_start": can_start,
        "can_manage_attendance": can_manage_attendance,
        "can_propose_agenda": can_propose and not can_create_directly,
        "can_approve_proposals": checker.can_approve_agenda_items() or view.membership.has_permission("agenda.manage"),
        "pending_proposals": pending_proposals,
        "protocol_entries": protocol_entries,
        "protocol_entry_count": protocol_entry_count,
        "available_members": available_members,
        "status_choices": FactionMeeting.STATUS_CHOICES,
        "is_creator": meeting.created_by == view.membership,
        "organization": view.organization,
        "org_slug": view.organization.slug,
        "membership": view.membership,
    }


def _render_partial(template_name, context, request=None):
    """Render a template partial to string."""
    return render_to_string(template_name, context, request=request)


def _htmx_response(html, trigger=None, refresh=False):
    """Build an HTMX response with optional triggers."""
    response = HttpResponse(html)
    if trigger:
        response["HX-Trigger"] = trigger
    if refresh:
        response["HX-Refresh"] = "true"
    return response


def _apply_approval_item_decision(agenda_item, decision, meeting, membership):
    """
    Genehmigungs-TOP auswerten: eine angenommene Abstimmung auf dem
    automatischen ersten TOP genehmigt das Protokoll der vorherigen Sitzung
    (ProtocolApprovalService setzt Status, Flag und Genehmigungs-Metadaten).
    """
    if not agenda_item.is_approval_item or not agenda_item.approves_meeting_id:
        return False
    if decision is None or not decision.passed:
        return False

    from ..services import ProtocolApprovalService

    return ProtocolApprovalService.approve_protocol(
        agenda_item.approves_meeting,
        approved_in_meeting=meeting,
        approved_by=membership,
    )


def _renumber_items(meeting, visibility):
    """Renumber items after reordering to maintain consistent numbering."""
    items = meeting.agenda_items.filter(visibility=visibility, parent__isnull=True, is_approval_item=False).order_by(
        "order"
    )

    prefix = "NÖ " if visibility == "internal" else ""
    start_num = 1

    if visibility == "public" and meeting.agenda_items.filter(is_approval_item=True).exists():
        start_num = 2

    for i, item in enumerate(items, start=start_num):
        new_number = f"{prefix}{i}"
        if item.number != new_number:
            item.number = new_number
            item.save(update_fields=["number"])

        for j, child in enumerate(item.children.order_by("order"), start=1):
            child_number = f"{new_number}.{j}"
            if child.number != child_number:
                child.number = child_number
                child.save(update_fields=["number"])


# ---------------------------------------------------------------------------
# 1. List + Create
# ---------------------------------------------------------------------------
