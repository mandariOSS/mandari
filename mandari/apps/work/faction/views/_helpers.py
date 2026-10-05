# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsame Hilfen der Fraktionssitzungs-Views: Sitzungskontext, Teil-Rendering, HTMX-Antworten, TOP-Nummerierung."""

import logging
from datetime import timedelta

from django.db.models import Q
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

    from ..visibility import LOCKED_PLACEHOLDER, visible_children
    from ..visibility import can_view_internal as _can_view_internal

    checker = PermissionChecker(view.membership)

    # Agenda items (top-level only, children via prefetch) — Vorschläge stehen erst nach der
    # Genehmigung auf der Tagesordnung (Issue #872), offene erscheinen nur unter „Offene Vorschläge“
    agenda_items = (
        meeting.agenda_items.filter(parent__isnull=True, proposal_status="active")
        .select_related("related_agenda_item", "approves_meeting", "decision")
        .prefetch_related(
            "protocol_entries", "protocol_entries__speaker__user", "protocol_entries__action_assignee__user", "children"
        )
        .order_by("order", "number")
    )

    public_items = [i for i in agenda_items if i.visibility == "public"]
    can_view_internal = _can_view_internal(view.membership)
    # NÖ strikt (Issue #64): Nicht-Vereidigte erhalten KEINE NÖ-Objekte im
    # Kontext — nur die Anzahl für "Gesperrte Information"-Platzhalter
    internal_items = [i for i in agenda_items if i.visibility == "internal"] if can_view_internal else []
    locked_internal_count = 0 if can_view_internal else sum(1 for i in agenda_items if i.visibility == "internal")
    # Unterpunkte: NÖ-Unterpunkte öffentlicher TOPs nur für Vereidigte (Children sind vorab geladen)
    for item in agenda_items:
        item.visible_children = visible_children(item, include_internal=can_view_internal)

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

    # Nachträge (Issue #63): nach endgültiger Genehmigung dürfen
    # Protokollant/Vorsitz sichtbare Nachträge erfassen
    can_add_addendum = meeting.protocol_approved and (
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

    # Teilnahme-Workflow (Issue #67): finale Bestätigung durch den Vorstand
    from ..invitations import can_confirm_attendance as _can_confirm

    attendance_confirmed = meeting.attendance_confirmed_at is not None
    can_confirm_attendance = (
        meeting.status == "completed" and not attendance_confirmed and _can_confirm(view.membership)
    )
    if attendance_confirmed:
        # Gesperrt: keine Anwesenheitspflege mehr nach der Bestätigung
        can_manage_attendance = False

    can_propose = checker.can_propose_agenda_items()
    can_create_directly = checker.can_create_agenda_items_directly()
    # TOPs direkt eintragen (Issue #872): Verwaltung der Sitzung oder agenda.create (Ratsmitglieder)
    from ..agenda import AGENDA_OPEN_STATUSES, PROPOSAL_OPEN_STATUSES

    can_add_items = can_edit or (can_create_directly and meeting.status in AGENDA_OPEN_STATUSES)

    # Beschlussfähigkeit (Issue #69): Anzeige während/nach der Sitzung
    from ..quorum import faction_quorum_status

    quorum = faction_quorum_status(meeting) if meeting.status in ("ongoing", "completed") else None

    # Einladungslogik (Issue #62): Freigabe-Status für die Sidebar
    from ..invitations import can_release_invitations as _can_release
    from ..invitations import get_invitation_settings, invitation_dispatch_at

    inv_settings = get_invitation_settings(view.organization)
    invitation_release_pending = (
        inv_settings["invitation_dispatch"] == "approval"
        and not meeting.invitation_sent
        and meeting.invitation_released_at is None
        and meeting.status in ("draft", "planned")
    )

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
        protocol_entries_qs = protocol_entries_qs.exclude(
            Q(agenda_item__visibility="internal") | Q(agenda_item__parent__visibility="internal")
        )
    protocol_entries = protocol_entries_qs[:10]

    protocol_entry_count = protocol_entries_qs.count()

    # TOP-Vorschläge: NÖ-Vorschläge sind für Nicht-Vereidigte unsichtbar
    pending_proposals = meeting.agenda_items.filter(proposal_status="proposed").select_related("proposed_by__user")
    if not can_view_internal:
        pending_proposals = pending_proposals.exclude(visibility="internal")
    # Eigene Vorschläge mit Stand (offen/abgelehnt) für die Vorschlagenden
    my_proposals = meeting.agenda_items.filter(
        proposed_by=view.membership, proposal_status__in=["proposed", "rejected"]
    ).order_by("proposed_at")

    return {
        "meeting": meeting,
        # NÖ strikt: Nicht-Vereidigte bekommen keine NÖ-Objekte in den Kontext
        "agenda_items": list(agenda_items) if can_view_internal else public_items,
        "public_agenda_items": public_items,
        "internal_agenda_items": internal_items,
        "can_view_internal": can_view_internal,
        # Nichtöffentliche Unterlage (PDF) einlesen und TOPs vorschlagen (Issue #873)
        "can_import_internal_documents": _can_import_internal(view.membership, meeting),
        "locked_internal_count": locked_internal_count,
        "locked_placeholder": LOCKED_PLACEHOLDER,
        "attendances": attendances,
        "my_attendance": my_attendance,
        "attendance_stats": attendance_stats,
        "can_edit": can_edit,
        "is_protocol_phase": is_protocol_phase,
        "can_protocol": can_protocol,
        "can_add_addendum": can_add_addendum,
        "can_start": can_start,
        "can_manage_attendance": can_manage_attendance,
        "attendance_confirmed": attendance_confirmed,
        "can_confirm_attendance": can_confirm_attendance,
        "quorum": quorum,
        "invitation_release_pending": invitation_release_pending,
        "can_release_invitations": _can_release(view.membership),
        "invitation_dispatch_at": invitation_dispatch_at(meeting, inv_settings),
        "invitation_settings": inv_settings,
        "can_add_items": can_add_items,
        "can_propose_agenda": can_propose and not can_create_directly and meeting.status in PROPOSAL_OPEN_STATUSES,
        "can_approve_proposals": checker.can_approve_agenda_items(),
        "pending_proposals": pending_proposals,
        "my_proposals": my_proposals,
        "protocol_entries": protocol_entries,
        "protocol_entry_count": protocol_entry_count,
        "available_members": available_members,
        "status_choices": FactionMeeting.STATUS_CHOICES,
        "is_creator": meeting.created_by == view.membership,
        "organization": view.organization,
        "org_slug": view.organization.slug,
        "membership": view.membership,
    }


def _can_import_internal(membership, meeting) -> bool:
    from ..internal_documents import can_import, is_editable

    return is_editable(meeting) and can_import(membership, meeting)


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
    """Renumber items after reordering — offene und abgelehnte Vorschläge bleiben ohne Nummer (Issue #872)."""
    from ..agenda import renumber

    renumber(meeting, visibility)


# ---------------------------------------------------------------------------
# 1. List + Create
# ---------------------------------------------------------------------------
