# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO Data Export Service.

Comprehensive GDPR Art. 15/20 data export covering all personal data
stored in Mandari, with JSON and PDF output formats.
"""

import json as json_mod
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


# =============================================================================
# Einordnung aller Personenbezüge in Work
# =============================================================================
#
# Jeder Verweis eines Work-Modells auf die Mitgliedschaft oder das Konto (``Modell.feld``) steht
# entweder in IM_EXPORT (mit dem Abschnitt, der ihn abdeckt) oder in NICHT_IM_EXPORT (mit
# Begründung). ``organization/tests/test_dsgvo_export_vollstaendig.py`` prüft das gegen die
# Modelle: Ein neues Modell mit Personenbezug lässt den Test scheitern, bis es hier eingeordnet ist.


@dataclass(frozen=True)
class Vermerk:
    """Bearbeitungsvermerk: Die Person hat etwas getan, der Inhalt gehört der Organisation."""

    ref: str  # "Modell.feld" in der App work
    area: str
    action: str
    org_path: str  # Lookup vom Modell zur Organisation
    when: str  # Zeitstempel-Attribut
    describe: Callable[[Any], str]
    select: tuple[str, ...] = ()
    exclude: dict[str, Any] = field(default_factory=dict)


def _motion_title(obj: Any) -> str:
    return str(obj.motion.title)


VERMERKE: tuple[Vermerk, ...] = (
    # Fraktionssitzungen
    Vermerk(
        "FactionMeeting.created_by", "Fraktion", "Sitzung angelegt", "organization", "created_at", lambda o: o.title
    ),
    Vermerk(
        "FactionMeeting.invitation_released_by",
        "Fraktion",
        "Einladung freigegeben",
        "organization",
        "invitation_released_at",
        lambda o: o.title,
    ),
    Vermerk(
        "FactionMeeting.attendance_confirmed_by",
        "Fraktion",
        "Anwesenheit bestätigt",
        "organization",
        "attendance_confirmed_at",
        lambda o: o.title,
    ),
    Vermerk(
        "FactionMeeting.protocol_approved_by",
        "Fraktion",
        "Protokoll genehmigt",
        "organization",
        "protocol_approved_at",
        lambda o: o.title,
    ),
    Vermerk(
        "FactionAgendaItem.reviewed_by",
        "Fraktion",
        "TOP-Vorschlag geprüft",
        "meeting__organization",
        "reviewed_at",
        lambda o: f"{o.meeting.title}: TOP {o.number}",
        select=("meeting",),
    ),
    Vermerk(
        "FactionAgendaItemAttachment.uploaded_by",
        "Fraktion",
        "Anhang an TOP hochgeladen",
        "agenda_item__meeting__organization",
        "created_at",
        lambda o: f"{o.agenda_item.meeting.title}: {o.filename}",
        select=("agenda_item__meeting",),
    ),
    Vermerk(
        "FactionAttendance.confirmed_final_by",
        "Fraktion",
        "Teilnahme endgültig bestätigt",
        "meeting__organization",
        "confirmed_final_at",
        lambda o: o.meeting.title,
        select=("meeting",),
    ),
    Vermerk(
        "FactionAttendanceCertificate.issued_by",
        "Fraktion",
        "Teilnahmebescheinigung ausgestellt",
        "organization",
        "issued_at",
        lambda o: f"{_date(o.period_start)} – {_date(o.period_end)}",
    ),
    Vermerk(
        "FactionDecision.recorded_by",
        "Fraktion",
        "Abstimmungsergebnis erfasst",
        "agenda_item__meeting__organization",
        "created_at",
        lambda o: f"{o.agenda_item.meeting.title}: TOP {o.agenda_item.number}",
        select=("agenda_item__meeting",),
    ),
    Vermerk(
        "FactionProtocolEntry.created_by",
        "Fraktion",
        "Protokolleintrag erfasst",
        "meeting__organization",
        "created_at",
        lambda o: f"{o.meeting.title}: {o.get_entry_type_display()}",
        select=("meeting",),
    ),
    Vermerk(
        "FactionAuditLog.membership",
        "Fraktion",
        "Änderung protokolliert",
        "organization",
        "created_at",
        lambda o: f"{o.action}: {o.object_repr}" + (f" (IP {o.ip_address})" if o.ip_address else ""),
    ),
    # Sitzungsvorbereitung
    Vermerk(
        "MeetingPreparation.prepared_by",
        "Sitzungsvorbereitung",
        "Als vorbereitet markiert",
        "organization",
        "prepared_at",
        lambda o: str(o.meeting),
        select=("meeting",),
    ),
    Vermerk(
        "AgendaItemPosition.set_by",
        "Sitzungsvorbereitung",
        "Fraktionsposition gesetzt",
        "organization",
        "updated_at",
        lambda o: f"{o.agenda_item}: {o.get_position_display()}",
        select=("agenda_item",),
    ),
    # Dokumente
    Vermerk(
        "MotionRevision.changed_by",
        "Dokumente",
        "Version gespeichert",
        "motion__organization",
        "created_at",
        lambda o: f"{o.motion.title} (Version {o.version})",
        select=("motion",),
    ),
    Vermerk(
        "MotionDocument.uploaded_by",
        "Dokumente",
        "Anhang hochgeladen",
        "motion__organization",
        "uploaded_at",
        lambda o: f"{o.motion.title}: {o.filename}",
        select=("motion",),
    ),
    Vermerk(
        "MotionEmailSubmission.submitted_by",
        "Dokumente",
        "Per E-Mail bei der Verwaltung eingereicht",
        "motion__organization",
        "sent_at",
        lambda o: f"{o.motion.title}: {o.subject}",
        select=("motion",),
    ),
    Vermerk(
        "MotionChecklistItem.completed_by",
        "Dokumente",
        "Checklistenpunkt erledigt",
        "motion__organization",
        "completed_at",
        lambda o: f"{o.motion.title}: {o.title}",
        select=("motion",),
    ),
    Vermerk(
        "MotionComment.resolved_by",
        "Dokumente",
        "Kommentar als erledigt markiert",
        "motion__organization",
        "resolved_at",
        _motion_title,
        select=("motion",),
    ),
    Vermerk(
        "MotionShare.created_by",
        "Dokumente",
        "Dokument freigegeben",
        "motion__organization",
        "created_at",
        lambda o: f"{o.motion.title} ({o.get_level_display()})",
        select=("motion",),
    ),
    Vermerk(
        "MotionShare.user",
        "Dokumente",
        "Dokument für dich freigegeben",
        "motion__organization",
        "created_at",
        lambda o: f"{o.motion.title} ({o.get_level_display()})",
        select=("motion",),
    ),
    Vermerk(
        "FolderGuestShare.created_by",
        "Dokumente",
        "Ordner freigegeben",
        "folder__organization",
        "created_at",
        lambda o: f"{o.folder.name} ({o.get_level_display()})",
        select=("folder",),
    ),
    Vermerk(
        "FolderGuestShare.user",
        "Dokumente",
        "Ordner für dich freigegeben",
        "folder__organization",
        "created_at",
        lambda o: f"{o.folder.name} ({o.get_level_display()})",
        select=("folder",),
    ),
    Vermerk(
        "DocumentFolder.created_by", "Dokumente", "Ordner angelegt", "organization", "created_at", lambda o: o.name
    ),
    Vermerk(
        "AdministrationConnection.connected_by",
        "Dokumente",
        "Verbindung zur Verwaltung eingerichtet",
        "organization",
        "connected_at",
        lambda o: "Digitale Einreichung",
    ),
    # Aufgaben
    Vermerk(
        "TaskActivity.actor",
        "Aufgaben",
        "Aufgabe bearbeitet",
        "task__organization",
        "created_at",
        lambda o: f"{o.task.title}: {o.get_activity_type_display()}",
        select=("task",),
        exclude={"activity_type": "comment"},  # Kommentare stehen mit Inhalt bei den Aufgaben
    ),
    Vermerk(
        "TaskAttachment.uploaded_by",
        "Aufgaben",
        "Anhang hochgeladen",
        "task__organization",
        "created_at",
        lambda o: f"{o.task.title}: {o.filename}",
        select=("task",),
    ),
    Vermerk(
        "TaskShare.shared_by",
        "Aufgaben",
        "Aufgabe freigegeben",
        "task__organization",
        "shared_at",
        lambda o: o.task.title,
        select=("task",),
    ),
    # Organisation
    Vermerk(
        "MemberAbsence.deputy",
        "Organisation",
        "Als Vertretung eingetragen",
        "organization",
        "created_at",
        lambda o: f"{_date(o.start_date)} – {_date(o.end_date)}",
    ),
    Vermerk(
        "MemberChangeRequest.decided_by",
        "Organisation",
        "Änderungsantrag entschieden",
        "organization",
        "decided_at",
        lambda o: o.get_request_type_display(),
    ),
)

#: Verweise, die ein eigener Abschnitt des Exports abdeckt (Modell.feld → Abschnitt)
IM_EXPORT: dict[str, str] = {
    "Task.created_by": "tasks",
    "Task.assigned_to": "tasks",
    "TaskShare.membership": "tasks",
    "Motion.author": "motions",
    "MotionApproval.approver": "motions / document_involvement",
    "MotionComment.author": "motions / document_involvement",
    "Motion.responsible": "document_involvement",
    "Motion.contributors": "document_involvement",
    "FactionAttendance.membership": "faction_attendance",
    "FactionAgendaItem.proposed_by": "faction.proposals",
    "FactionProtocolEntry.speaker": "faction.protocol_entries",
    "FactionProtocolEntry.action_assignee": "faction.protocol_entries",
    "FactionAttendanceCertificate.membership": "faction.certificates",
    "MeetingPreparation.membership": "meetings.preparations",
    "AgendaItemNote.author": "meetings.agenda_notes",
    "AgendaSpeechNote.author": "meetings.speech_notes",
    "PaperComment.author": "meetings.paper_comments",
    "AgendaPrivateNote.author": "meetings.private_notes",
    "FileAnnotation.author": "meetings.file_annotations",
    "AgendaSupplementaryDocument.added_by": "meetings.added_documents",
    "MemberAbsence.membership": "absences",
    "MemberChangeRequest.requester": "change_requests",
    "Notification.recipient": "notifications",
    "NotificationPreference.membership": "notifications.preferences",
    "SupportTicket.created_by": "support",
    "SupportTicketMessage.author_membership": "support / support_messages",
    "DataExport.membership": "account_extras.data_exports",
    "CalendarFeedToken.user": "account_extras.calendar_feed",
    "ArticleFeedback.user": "account_extras.knowledge_base_feedback",
    **{vermerk.ref: "vermerke" for vermerk in VERMERKE},
}

#: Bewusst nicht exportierte Verweise mit Begründung
NICHT_IM_EXPORT: dict[str, str] = {
    "TaskComment.author": (
        "Altmodell: Aufgabenkommentare liegen seit Migration 0027 als Aufgaben-Aktivität (Typ Kommentar) "
        "vor und stehen von dort mit Inhalt bei den Aufgaben."
    ),
    "Notification.actor": (
        "Benachrichtigungen an andere Personen; der auslösende Vorgang steht im jeweiligen Abschnitt."
    ),
    "KnowledgeBaseArticle.author": "Artikel der Wissensdatenbank verfasst der Plattform-Support, keine Mitglieder.",
    "SupportTicket.assigned_to": "Bearbeitung durch den Plattform-Support (Mitarbeiterkonto), keine Mitglieder.",
    "SupportTicketMessage.author_staff": (
        "Antworten des Plattform-Supports; sie stehen im Ticket der anfragenden Person."
    ),
}


class DsgvoExportService:
    """Service for generating comprehensive DSGVO data exports."""

    def collect_user_data(self, user, membership, organization) -> dict:
        """
        Collect ALL personal data for a user across all categories.

        Returns a dict with all data categories suitable for JSON or PDF export
        (Einordnung aller Personenbezüge: IM_EXPORT / NICHT_IM_EXPORT).
        """
        now = timezone.now()

        return {
            "meta": {
                "export_date": now.isoformat(),
                "export_date_display": now.strftime("%d.%m.%Y um %H:%M Uhr"),
                "format": "DSGVO Art. 15/20 Datenexport",
                "organization": organization.name,
            },
            "account": self._collect_account(user),
            "membership": self._collect_membership(membership),
            "security_sessions": self._collect_sessions(user),
            "security_logins": self._collect_login_attempts(user),
            "security_2fa": self._collect_2fa(user),
            "security_alerts": self._collect_security_alerts(user),
            "account_extras": self._collect_account_extras(user, membership),
            "tasks": self._collect_tasks(membership, organization),
            "motions": self._collect_motions(membership, organization),
            "document_involvement": self._collect_document_involvement(membership, organization),
            "faction_attendance": self._collect_faction_attendance(membership, organization),
            "faction": self._collect_faction(membership, organization),
            "meetings": self._collect_meeting_data(membership, organization),
            "absences": self._collect_absences(membership, organization),
            "change_requests": self._collect_change_requests(membership, organization),
            "notifications": self._collect_notifications(membership),
            "support": self._collect_support(membership, organization),
            "support_messages": self._collect_support_messages(membership, organization),
            "vermerke": self._collect_vermerke(membership, organization),
        }

    # -------------------------------------------------------------------------
    # Account & Membership
    # -------------------------------------------------------------------------

    def _collect_account(self, user) -> dict:
        return {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone or "",
            "avatar": "Vorhanden" if user.avatar else "Keines",
            "date_joined": _dt(user.date_joined),
            "last_login": _dt(user.last_login),
            "email_verified": user.email_verified,
            "settings": user.settings or {},
        }

    def _collect_membership(self, membership) -> dict:
        return {
            "id": str(membership.id),
            "joined_at": _dt(membership.joined_at),
            "is_sworn_in": membership.is_sworn_in,
            "is_active": membership.is_active,
            "roles": [r.name for r in membership.roles.all()],
            "committees": [c.name for c in membership.oparl_committees.all()],
            "individual_permissions": [p.codename for p in membership.individual_permissions.all()],
            "denied_permissions": [p.codename for p in membership.denied_permissions.all()],
        }

    # -------------------------------------------------------------------------
    # Security
    # -------------------------------------------------------------------------

    def _collect_sessions(self, user) -> list:
        from apps.accounts.models import UserSession

        return [
            {
                "device_name": s.device_name,
                "user_agent": s.user_agent,
                "ip_address": s.ip_address or "",
                "location": s.location,
                "is_current": s.is_current,
                "created_at": _dt(s.created_at),
                "last_activity": _dt(s.last_activity),
            }
            for s in UserSession.objects.filter(user=user).order_by("-created_at")
        ]

    def _collect_login_attempts(self, user) -> list:
        from apps.accounts.models import LoginAttempt

        return [
            {
                "ip_address": a.ip_address,
                "user_agent": a.user_agent,
                "was_successful": a.was_successful,
                "failure_reason": a.failure_reason,
                "timestamp": _dt(a.timestamp),
            }
            for a in LoginAttempt.objects.filter(email=user.email).order_by("-timestamp")
        ]

    def _collect_2fa(self, user) -> dict | None:
        """Collect 2FA metadata. Secrets and backup codes are NOT exported."""
        try:
            device = user.totp_device
        except Exception:
            return None

        return {
            "is_confirmed": device.is_confirmed,
            "is_active": device.is_active,
            "created_at": _dt(device.created_at),
            "confirmed_at": _dt(device.confirmed_at),
            "last_used_at": _dt(device.last_used_at),
            "has_backup_codes": bool(device.backup_codes_encrypted),
            # Secrets and backup codes deliberately excluded for security
        }

    def _collect_security_alerts(self, user) -> list:
        from apps.accounts.models import SecurityNotification

        return [
            {
                "type": a.notification_type,
                "title": a.title,
                "message": a.message,
                "ip_address": a.ip_address or "",
                "device_info": a.device_info,
                "location": a.location,
                "is_read": a.is_read,
                "created_at": _dt(a.created_at),
            }
            for a in SecurityNotification.objects.filter(user=user).order_by("-created_at")
        ]

    def _collect_account_extras(self, user, membership) -> dict:
        """Kalender-Abo (ohne Zugangsschlüssel), eigene Datenexporte, Rückmeldungen zur Wissensdatenbank."""
        from apps.work.faction.models import CalendarFeedToken
        from apps.work.organization.models import DataExport
        from apps.work.support.models import ArticleFeedback

        feed = CalendarFeedToken.objects.filter(user=user).first()
        return {
            "calendar_feed": (
                {"created_at": _dt(feed.created_at), "regenerated_at": _dt(feed.regenerated_at)} if feed else None
            ),
            "data_exports": [
                {
                    "format": e.export_format,
                    "status": e.status,
                    "created_at": _dt(e.created_at),
                    "completed_at": _dt(e.completed_at),
                }
                for e in DataExport.objects.filter(membership=membership).order_by("-created_at")
            ],
            "knowledge_base_feedback": [
                {
                    "article": f.article.title,
                    "is_helpful": f.is_helpful,
                    "comment": f.comment or "",
                    "created_at": _dt(f.created_at),
                }
                for f in ArticleFeedback.objects.filter(user=user).select_related("article").order_by("-created_at")
            ],
        }

    # -------------------------------------------------------------------------
    # Tasks
    # -------------------------------------------------------------------------

    def _collect_tasks(self, membership, organization) -> list:
        """
        Aufgaben, die die Person angelegt hat, die ihr zugewiesen oder freigegeben sind oder die sie
        kommentiert hat – mit ihren Kommentaren (Aufgaben-Aktivität vom Typ Kommentar).
        """
        from django.db.models import Q

        from apps.work.tasks.models import Task, TaskActivity, TaskShare

        own_comments = TaskActivity.objects.filter(
            actor=membership, activity_type="comment", task__organization=organization
        ).order_by("created_at")
        comments_by_task: dict[Any, list[dict]] = {}
        for c in own_comments:
            comments_by_task.setdefault(c.task_id, []).append({"content": c.content, "created_at": _dt(c.created_at)})

        shared_ids = set(
            TaskShare.objects.filter(membership=membership, task__organization=organization).values_list(
                "task_id", flat=True
            )
        )
        tasks = (
            Task.objects.filter(organization=organization)
            .filter(
                Q(created_by=membership)
                | Q(assigned_to=membership)
                | Q(id__in=shared_ids)
                | Q(id__in=list(comments_by_task))
            )
            .distinct()
            .order_by("-created_at")
        )

        result = []
        for t in tasks:
            is_creator = t.created_by_id == membership.id
            is_assignee = t.assigned_to_id == membership.id
            is_shared = t.id in shared_ids
            result.append(
                {
                    "title": t.title,
                    # Beschreibung nur bei eigener Beteiligung, nicht allein wegen eines Kommentars
                    "description": (t.description or "") if (is_creator or is_assignee or is_shared) else "",
                    "status": t.status,
                    "priority": t.priority,
                    "due_date": _date(t.due_date),
                    "is_creator": is_creator,
                    "is_assignee": is_assignee,
                    "is_shared_with": is_shared,
                    "created_at": _dt(t.created_at),
                    "comments": comments_by_task.get(t.id, []),
                }
            )

        return result

    # -------------------------------------------------------------------------
    # Motions
    # -------------------------------------------------------------------------

    def _collect_motions(self, membership, organization) -> list:
        from apps.work.motions.models import (
            Motion,
            MotionApproval,
            MotionComment,
            MotionDocument,
            MotionRevision,
            MotionShare,
        )

        motions = Motion.objects.filter(organization=organization, author=membership).order_by("-created_at")

        result = []
        for m in motions:
            # Decrypt content safely
            content = _safe_decrypt(m, "content")

            revisions = MotionRevision.objects.filter(motion=m).order_by("version")
            comments = MotionComment.objects.filter(motion=m, author=membership).order_by("created_at")
            approvals = MotionApproval.objects.filter(motion=m).select_related("approver__user")
            shares = MotionShare.objects.filter(motion=m).order_by("-created_at")
            documents = MotionDocument.objects.filter(motion=m).order_by("-uploaded_at")

            result.append(
                {
                    "title": m.title,
                    "status": m.status,
                    "summary": m.summary or "",
                    "content": content,
                    "visibility": m.visibility,
                    "created_at": _dt(m.created_at),
                    "submitted_at": _dt(m.submitted_at),
                    "revisions": [
                        {
                            "version": r.version,
                            "content": _safe_decrypt(r, "content"),
                            "change_summary": r.change_summary or "",
                            "changed_by": (r.changed_by.user.get_full_name() if r.changed_by else ""),
                            "created_at": _dt(r.created_at),
                        }
                        for r in revisions
                    ],
                    "comments": [
                        {
                            "content": c.content,
                            "created_at": _dt(c.created_at),
                        }
                        for c in comments
                    ],
                    "approvals": [
                        {
                            "approver": (a.approver.user.get_full_name() if a.approver else ""),
                            "approval_type": a.approval_type,
                            "approved": a.approved,
                            "comment": a.comment or "",
                            "decided_at": _dt(a.decided_at),
                        }
                        for a in approvals
                    ],
                    "shares": [
                        {
                            "scope": s.scope,
                            "level": s.level,
                            "message": s.message or "",
                            "created_at": _dt(s.created_at),
                        }
                        for s in shares
                    ],
                    "documents": [
                        {
                            "filename": d.filename,
                            "mime_type": d.mime_type,
                            "file_size": d.file_size,
                            "uploaded_at": _dt(d.uploaded_at),
                        }
                        for d in documents
                    ],
                }
            )

        return result

    def _collect_document_involvement(self, membership, organization) -> list:
        """
        Dokumente anderer, an denen die Person beteiligt ist: Federführung, Mitarbeit, eigene
        Kommentare und Freigabe-Entscheidungen. Den Dokumentinhalt enthält nur der Abschnitt
        der eigenen Dokumente.
        """
        from django.db.models import Q

        from apps.work.motions.models import Motion, MotionApproval, MotionComment

        comments = (
            MotionComment.objects.filter(author=membership, motion__organization=organization)
            .exclude(motion__author=membership)
            .order_by("created_at")
        )
        approvals = (
            MotionApproval.objects.filter(approver=membership, motion__organization=organization)
            .exclude(motion__author=membership)
            .order_by("created_at")
        )
        comments_by_motion: dict[Any, list[dict]] = {}
        for c in comments:
            comments_by_motion.setdefault(c.motion_id, []).append(
                {
                    "content": c.content,
                    "selected_text": c.selected_text or "",
                    "is_resolved": c.is_resolved,
                    "created_at": _dt(c.created_at),
                }
            )
        approvals_by_motion: dict[Any, list[dict]] = {}
        for a in approvals:
            approvals_by_motion.setdefault(a.motion_id, []).append(
                {
                    "approval_type": a.approval_type,
                    "approved": a.approved,
                    "comment": a.comment or "",
                    "created_at": _dt(a.created_at),
                    "decided_at": _dt(a.decided_at),
                }
            )

        motions = (
            Motion.objects.filter(organization=organization)
            .exclude(author=membership)
            .filter(
                Q(responsible=membership)
                | Q(contributors=membership)
                | Q(id__in=list(comments_by_motion))
                | Q(id__in=list(approvals_by_motion))
            )
            .distinct()
            .order_by("-created_at")
        )
        contributor_ids = set(membership.contributing_motions.values_list("id", flat=True))
        result = []
        for m in motions:
            roles = []
            if m.responsible_id == membership.id:
                roles.append("Federführung")
            if m.id in contributor_ids:
                roles.append("Mitarbeit")
            result.append(
                {
                    "title": m.title,
                    "status": m.status,
                    "roles": roles,
                    "comments": comments_by_motion.get(m.id, []),
                    "approvals": approvals_by_motion.get(m.id, []),
                }
            )
        return result

    # -------------------------------------------------------------------------
    # Faction Attendance
    # -------------------------------------------------------------------------

    def _collect_faction_attendance(self, membership, organization) -> list:
        from apps.work.faction.models import FactionAttendance

        attendances = (
            FactionAttendance.objects.filter(membership=membership, meeting__organization=organization)
            .select_related("meeting")
            .order_by("-meeting__start")
        )

        return [
            {
                "meeting": a.meeting.title,
                "date": _dt(a.meeting.start),
                "status": a.status,
                "response_message": a.response_message or "",
                "guest_name": a.guest_name if a.is_guest else "",
                "checked_in_at": _dt(a.checked_in_at),
                "checked_out_at": _dt(a.checked_out_at),
            }
            for a in attendances
        ]

    def _collect_faction(self, membership, organization) -> dict:
        """Eigene TOP-Vorschläge, Protokolleinträge über die Person und ihre Teilnahmebescheinigungen."""
        from django.db.models import Q

        from apps.work.faction.models import FactionAgendaItem, FactionAttendanceCertificate, FactionProtocolEntry
        from apps.work.faction.visibility import can_view_item_with_parents

        proposals = [
            {
                "meeting": i.meeting.title,
                "title": i.title,
                "status": i.proposal_status,
                "rejection_reason": i.rejection_reason or "",
                "proposed_at": _dt(i.proposed_at),
            }
            for i in FactionAgendaItem.objects.filter(proposed_by=membership, meeting__organization=organization)
            .select_related("meeting")
            .order_by("-created_at")
        ]

        entries = []
        for e in (
            FactionProtocolEntry.objects.filter(meeting__organization=organization)
            .filter(Q(speaker=membership) | Q(action_assignee=membership))
            .select_related("meeting", "agenda_item")
            .order_by("-created_at")
        ):
            # NÖ strikt (Issue #64): Inhalt nichtöffentlicher TOPs nur, wenn die Person ihn sehen darf
            visible = e.agenda_item is None or can_view_item_with_parents(e.agenda_item, membership)
            entries.append(
                {
                    "meeting": e.meeting.title,
                    "type": e.get_entry_type_display(),
                    "role": "Redebeitrag" if e.speaker_id == membership.id else "Aufgabe",
                    "content": _safe_decrypt(e, "content") if visible else "[nichtöffentlicher Tagesordnungspunkt]",
                    "action_due_date": _date(e.action_due_date),
                    "created_at": _dt(e.created_at),
                }
            )

        certificates = [
            {
                "period_start": _date(c.period_start),
                "period_end": _date(c.period_end),
                "attendance_count": c.attendance_count,
                "issued_at": _dt(c.issued_at),
            }
            for c in FactionAttendanceCertificate.objects.filter(
                membership=membership, organization=organization
            ).order_by("-issued_at")
        ]
        return {"proposals": proposals, "protocol_entries": entries, "certificates": certificates}

    # -------------------------------------------------------------------------
    # Meeting Preparation & Notes
    # -------------------------------------------------------------------------

    def _collect_meeting_data(self, membership, organization) -> dict:
        from apps.work.meetings.models import (
            AgendaItemNote,
            AgendaPrivateNote,
            AgendaSpeechNote,
            AgendaSupplementaryDocument,
            FileAnnotation,
            MeetingPreparation,
            PaperComment,
        )

        # Preparations
        preparations = (
            MeetingPreparation.objects.filter(membership=membership, organization=organization)
            .select_related("meeting")
            .order_by("-created_at")
        )

        preps = [
            {
                "meeting": str(p.meeting),
                "notes": _safe_decrypt(p, "notes"),
                "is_prepared": p.is_prepared,
                "prepared_at": _dt(p.prepared_at),
                "created_at": _dt(p.created_at),
            }
            for p in preparations
        ]

        # Agenda item notes
        notes = (
            AgendaItemNote.objects.filter(author=membership, organization=organization)
            .select_related("agenda_item")
            .order_by("-created_at")
        )

        agenda_notes = [
            {
                "agenda_item": str(n.agenda_item),
                "content": _safe_decrypt(n, "content"),
                "visibility": n.visibility,
                "is_decision": n.is_decision,
                "created_at": _dt(n.created_at),
            }
            for n in notes
        ]

        # Speech notes (plain text, not encrypted)
        speeches = (
            AgendaSpeechNote.objects.filter(author=membership, organization=organization)
            .select_related("meeting", "agenda_item")
            .order_by("-created_at")
        )

        speech_notes = [
            {
                "meeting": str(s.meeting),
                "agenda_item": str(s.agenda_item),
                "title": s.title,
                "content": s.content or "",
                "estimated_duration": s.estimated_duration,
                "created_at": _dt(s.created_at),
            }
            for s in speeches
        ]

        # Paper comments
        comments = (
            PaperComment.objects.filter(author=membership, organization=organization)
            .select_related("paper")
            .order_by("-created_at")
        )

        paper_comments = [
            {
                "paper": str(c.paper),
                "content": _safe_decrypt(c, "content"),
                "visibility": c.visibility,
                "is_recommendation": c.is_recommendation,
                "created_at": _dt(c.created_at),
            }
            for c in comments
        ]

        # Private Notizen zu TOPs (nur für die Person sichtbar)
        private_notes = [
            {
                "agenda_item": str(n.agenda_item),
                "content": _safe_decrypt(n, "content"),
                "created_at": _dt(n.created_at),
            }
            for n in AgendaPrivateNote.objects.filter(author=membership, organization=organization)
            .select_related("agenda_item")
            .order_by("-created_at")
        ]

        # Anmerkungen an Dateien (RIS-Datei oder eigene Anlage)
        file_annotations = [
            {
                "file": (
                    a.oparl_file.name or a.oparl_file.file_name
                    if a.oparl_file_id
                    else (a.supplementary_document.title if a.supplementary_document_id else "")
                ),
                "page": a.page,
                "content": _safe_decrypt(a, "content"),
                "created_at": _dt(a.created_at),
            }
            for a in FileAnnotation.objects.filter(author=membership, organization=organization)
            .select_related("oparl_file", "supplementary_document")
            .order_by("-created_at")
        ]

        # Selbst hinzugefügte Anlagen (Links, Uploads, RIS-Verweise)
        added_documents = [
            {
                "title": d.title,
                "document_type": d.document_type,
                "url": d.url or "",
                "filename": d.filename or "",
                "created_at": _dt(d.created_at),
            }
            for d in AgendaSupplementaryDocument.objects.filter(
                added_by=membership, organization=organization
            ).order_by("-created_at")
        ]

        return {
            "preparations": preps,
            "agenda_notes": agenda_notes,
            "speech_notes": speech_notes,
            "paper_comments": paper_comments,
            "private_notes": private_notes,
            "file_annotations": file_annotations,
            "added_documents": added_documents,
        }

    # -------------------------------------------------------------------------
    # Absences
    # -------------------------------------------------------------------------

    def _collect_absences(self, membership, organization) -> list:
        from .models import MemberAbsence

        absences = MemberAbsence.objects.filter(membership=membership, organization=organization).order_by(
            "-start_date"
        )

        return [
            {
                "start_date": _date(a.start_date),
                "end_date": _date(a.end_date),
                "reason": a.reason or "",
                "deputy": (a.deputy.user.get_full_name() if a.deputy else ""),
                "is_active": a.is_active,
                "created_at": _dt(a.created_at),
            }
            for a in absences
        ]

    # -------------------------------------------------------------------------
    # Change Requests
    # -------------------------------------------------------------------------

    def _collect_change_requests(self, membership, organization) -> list:
        from .models import MemberChangeRequest

        requests = MemberChangeRequest.objects.filter(requester=membership, organization=organization).order_by(
            "-created_at"
        )

        return [
            {
                "type": r.request_type,
                "status": r.status,
                "reason": r.reason or "",
                "request_data": r.request_data or {},
                "created_at": _dt(r.created_at),
                "decided_at": _dt(r.decided_at),
            }
            for r in requests
        ]

    # -------------------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------------------

    def _collect_notifications(self, membership) -> dict:
        from apps.work.notifications.models import Notification, NotificationPreference

        # All notifications (not limited to 100)
        notifications = Notification.objects.filter(recipient=membership).order_by("-created_at")

        notif_list = [
            {
                "type": n.notification_type,
                "title": n.title,
                "message": n.message or "",
                "link": n.link or "",
                "is_read": n.is_read,
                "created_at": _dt(n.created_at),
            }
            for n in notifications
        ]

        # Preferences
        preferences = None
        try:
            pref = NotificationPreference.objects.get(membership=membership)
            preferences = {
                "email_enabled": pref.email_enabled,
                "push_enabled": pref.push_enabled,
                "email_digest": pref.email_digest,
                "quiet_hours_enabled": pref.quiet_hours_enabled,
                "quiet_hours_start": str(pref.quiet_hours_start) if pref.quiet_hours_start else None,
                "quiet_hours_end": str(pref.quiet_hours_end) if pref.quiet_hours_end else None,
                "type_settings": pref.type_settings or {},
            }
        except Exception:
            pass

        return {
            "notifications": notif_list,
            "preferences": preferences,
        }

    # -------------------------------------------------------------------------
    # Support
    # -------------------------------------------------------------------------

    def _collect_support(self, membership, organization) -> list:
        from apps.work.support.models import SupportTicket

        tickets = SupportTicket.objects.filter(organization=organization, created_by=membership).order_by("-created_at")

        result = []
        for t in tickets:
            description = _safe_decrypt(t, "description")

            messages = t.messages.filter(is_internal=False).order_by("created_at")
            msg_list = []
            for msg in messages:
                msg_list.append(
                    {
                        "content": _safe_decrypt(msg, "content"),
                        "is_from_support": msg.is_from_support,
                        "created_at": _dt(msg.created_at),
                    }
                )

            result.append(
                {
                    "subject": t.subject,
                    "category": t.category,
                    "priority": t.priority,
                    "status": t.status,
                    "description": description,
                    "created_at": _dt(t.created_at),
                    "resolved_at": _dt(t.resolved_at),
                    "messages": msg_list,
                }
            )

        return result

    def _collect_support_messages(self, membership, organization) -> list:
        """Eigene Nachrichten in Tickets, die andere Mitglieder eröffnet haben."""
        from apps.work.support.models import SupportTicketMessage

        return [
            {
                "ticket": m.ticket.subject,
                "content": _safe_decrypt(m, "content"),
                "created_at": _dt(m.created_at),
            }
            for m in SupportTicketMessage.objects.filter(
                author_membership=membership, ticket__organization=organization
            )
            .exclude(ticket__created_by=membership)
            .select_related("ticket")
            .order_by("-created_at")
        ]

    # -------------------------------------------------------------------------
    # Bearbeitungsvermerke
    # -------------------------------------------------------------------------

    def _collect_vermerke(self, membership, organization) -> list:
        """Alle Bearbeitungsvermerke aus VERMERKE, neueste zuerst."""
        from django.apps import apps as django_apps

        from apps.tenants.models import Membership

        rows: list[tuple[Any, dict]] = []
        for vermerk in VERMERKE:
            model_name, field_name = vermerk.ref.split(".")
            model = django_apps.get_model("work", model_name)
            target = membership if model._meta.get_field(field_name).related_model is Membership else membership.user
            queryset = model.objects.filter(**{field_name: target, vermerk.org_path: organization})
            if vermerk.exclude:
                queryset = queryset.exclude(**vermerk.exclude)
            for obj in queryset.select_related(*vermerk.select):
                when = getattr(obj, vermerk.when)
                rows.append(
                    (
                        when,
                        {
                            "area": vermerk.area,
                            "action": vermerk.action,
                            "object": vermerk.describe(obj),
                            "at": _dt(when),
                        },
                    )
                )
        epoch = timezone.now().replace(year=1970, month=1, day=1)
        rows.sort(key=lambda row: row[0] or epoch, reverse=True)
        return [row for _when, row in rows]

    # -------------------------------------------------------------------------
    # Export Formats
    # -------------------------------------------------------------------------

    def export_to_json(self, data: dict) -> HttpResponse:
        """Export data as JSON download."""
        content = json_mod.dumps(data, indent=2, ensure_ascii=False, default=str)
        filename = f"mandari-datenexport-{timezone.now().strftime('%Y%m%d')}.json"
        response = HttpResponse(content, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def export_to_pdf(self, data: dict, user, organization) -> HttpResponse:
        """Export data as PDF download."""
        context = {
            "data": self.pdf_data(data),
            "user": user,
            "organization": organization,
            "export_date": timezone.now(),
        }

        html_content = render_to_string("work/profile/export/dsgvo_export.html", context)

        pdf_bytes = self._html_to_pdf(html_content)

        filename = f"mandari-datenexport-{timezone.now().strftime('%Y%m%d')}.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @staticmethod
    def pdf_data(data: dict) -> dict:
        """
        Daten für die HTML-/PDF-Ausgabe: Dokumentinhalte nur als bereinigtes Editor-HTML.

        Der JSON-Export bleibt unverändert vollständig; als HTML wird Inhalt dagegen nie
        ungeprüft ausgegeben (apps/work/sanitize.py).
        """
        from apps.work.sanitize import safe_editor_html

        motions = [{**m, "content": safe_editor_html(m.get("content"))} for m in data.get("motions") or []]
        return {**data, "motions": motions}

    def _html_to_pdf(self, html_content: str) -> bytes:
        """Convert HTML to PDF (gemeinsamer Baustein in apps/common/pdf.py)."""
        from apps.common.pdf import html_to_pdf

        return html_to_pdf(html_content)


# =============================================================================
# Helpers
# =============================================================================


def _dt(value) -> str | None:
    """Format a datetime as short German format for human-readable export."""
    if value is None:
        return None
    try:
        return value.strftime("%d.%m.%Y %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def _date(value) -> str | None:
    """Format a date as short German format."""
    if value is None:
        return None
    try:
        return value.strftime("%d.%m.%Y")
    except (AttributeError, ValueError):
        return str(value)


def _safe_decrypt(obj, field_name: str) -> str:
    """
    Entschlüsselten Wert lesen: ``get_<feld>_decrypted()`` des EncryptedTextField, sonst eine
    gleichnamige Property. Nicht jedes Modell hat die Property (z. B. Vorbereitungs- und private
    TOP-Notizen) – ohne den Rückgriff fehlten diese Inhalte stillschweigend im Export.
    """
    try:
        getter = getattr(obj, f"get_{field_name}_decrypted", None)
        if callable(getter):
            return getter() or ""
        return getattr(obj, field_name, "") or ""
    except Exception:
        return "[Inhalt konnte nicht entschlüsselt werden]"


# Singleton instance
dsgvo_export_service = DsgvoExportService()
