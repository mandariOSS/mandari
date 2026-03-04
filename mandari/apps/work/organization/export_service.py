# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO Data Export Service.

Comprehensive GDPR Art. 15/20 data export covering all personal data
stored in Mandari, with JSON and PDF output formats.
"""

import io
import json as json_mod
import logging

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


class DsgvoExportService:
    """Service for generating comprehensive DSGVO data exports."""

    def collect_user_data(self, user, membership, organization) -> dict:
        """
        Collect ALL personal data for a user across all categories.

        Returns a dict with 16 data categories suitable for JSON or PDF export.
        """
        now = timezone.now()

        data = {
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
            "security_trusted": self._collect_trusted_devices(user),
            "security_alerts": self._collect_security_alerts(user),
            "tasks": self._collect_tasks(membership, organization),
            "motions": self._collect_motions(membership, organization),
            "faction_attendance": self._collect_faction_attendance(membership, organization),
            "meetings": self._collect_meeting_data(membership, organization),
            "absences": self._collect_absences(membership, organization),
            "change_requests": self._collect_change_requests(membership, organization),
            "notifications": self._collect_notifications(membership),
            "support": self._collect_support(membership, organization),
        }

        return data

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
            "individual_permissions": [
                p.codename for p in membership.individual_permissions.all()
            ],
            "denied_permissions": [
                p.codename for p in membership.denied_permissions.all()
            ],
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

    def _collect_trusted_devices(self, user) -> list:
        from apps.accounts.models import TrustedDevice

        return [
            {
                "device_name": d.device_name,
                "user_agent": d.user_agent,
                "ip_address": d.ip_address or "",
                "created_at": _dt(d.created_at),
                "last_used_at": _dt(d.last_used_at),
                "expires_at": _dt(d.expires_at),
                "is_valid": d.is_valid,
            }
            for d in TrustedDevice.objects.filter(user=user)
        ]

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

    # -------------------------------------------------------------------------
    # Tasks
    # -------------------------------------------------------------------------

    def _collect_tasks(self, membership, organization) -> list:
        from django.db.models import Q

        from apps.work.tasks.models import Task, TaskComment

        tasks = (
            Task.objects.filter(organization=organization)
            .filter(Q(created_by=membership) | Q(assigned_to=membership))
            .order_by("-created_at")
        )

        result = []
        for t in tasks:
            comments = TaskComment.objects.filter(
                task=t, author=membership
            ).order_by("created_at")

            result.append(
                {
                    "title": t.title,
                    "description": t.description or "",
                    "status": t.status,
                    "priority": t.priority,
                    "due_date": _date(t.due_date),
                    "is_creator": t.created_by_id == membership.id,
                    "is_assignee": t.assigned_to_id == membership.id,
                    "created_at": _dt(t.created_at),
                    "comments": [
                        {
                            "content": c.content,
                            "created_at": _dt(c.created_at),
                        }
                        for c in comments
                    ],
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

        motions = Motion.objects.filter(
            organization=organization, author=membership
        ).order_by("-created_at")

        result = []
        for m in motions:
            # Decrypt content safely
            content = _safe_decrypt(m, "content")

            revisions = MotionRevision.objects.filter(motion=m).order_by("version")
            comments = MotionComment.objects.filter(
                motion=m, author=membership
            ).order_by("created_at")
            approvals = MotionApproval.objects.filter(motion=m).select_related(
                "approver__user"
            )
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
                            "changed_by": (
                                r.changed_by.user.get_full_name()
                                if r.changed_by
                                else ""
                            ),
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
                            "approver": (
                                a.approver.user.get_full_name()
                                if a.approver
                                else ""
                            ),
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

    # -------------------------------------------------------------------------
    # Faction Attendance
    # -------------------------------------------------------------------------

    def _collect_faction_attendance(self, membership, organization) -> list:
        from apps.work.faction.models import FactionAttendance

        attendances = (
            FactionAttendance.objects.filter(
                membership=membership, meeting__organization=organization
            )
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

    # -------------------------------------------------------------------------
    # Meeting Preparation & Notes
    # -------------------------------------------------------------------------

    def _collect_meeting_data(self, membership, organization) -> dict:
        from apps.work.meetings.models import (
            AgendaItemNote,
            AgendaSpeechNote,
            MeetingPreparation,
            PaperComment,
        )

        # Preparations
        preparations = (
            MeetingPreparation.objects.filter(
                membership=membership, organization=organization
            )
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
            AgendaItemNote.objects.filter(
                author=membership, organization=organization
            )
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
            AgendaSpeechNote.objects.filter(
                author=membership, organization=organization
            )
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
            PaperComment.objects.filter(
                author=membership, organization=organization
            )
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

        return {
            "preparations": preps,
            "agenda_notes": agenda_notes,
            "speech_notes": speech_notes,
            "paper_comments": paper_comments,
        }

    # -------------------------------------------------------------------------
    # Absences
    # -------------------------------------------------------------------------

    def _collect_absences(self, membership, organization) -> list:
        from .models import MemberAbsence

        absences = MemberAbsence.objects.filter(
            membership=membership, organization=organization
        ).order_by("-start_date")

        return [
            {
                "start_date": _date(a.start_date),
                "end_date": _date(a.end_date),
                "reason": a.reason or "",
                "deputy": (
                    a.deputy.user.get_full_name() if a.deputy else ""
                ),
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

        requests = MemberChangeRequest.objects.filter(
            requester=membership, organization=organization
        ).order_by("-created_at")

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
        notifications = Notification.objects.filter(
            recipient=membership
        ).order_by("-created_at")

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

        tickets = SupportTicket.objects.filter(
            organization=organization, created_by=membership
        ).order_by("-created_at")

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

    # -------------------------------------------------------------------------
    # Export Formats
    # -------------------------------------------------------------------------

    def export_to_json(self, data: dict) -> HttpResponse:
        """Export data as JSON download."""
        content = json_mod.dumps(data, indent=2, ensure_ascii=False, default=str)
        filename = f'mandari-datenexport-{timezone.now().strftime("%Y%m%d")}.json'
        response = HttpResponse(content, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def export_to_pdf(self, data: dict, user, organization) -> HttpResponse:
        """Export data as PDF download."""
        context = {
            "data": data,
            "user": user,
            "organization": organization,
            "export_date": timezone.now(),
        }

        html_content = render_to_string(
            "work/profile/export/dsgvo_export.html", context
        )

        pdf_bytes = self._html_to_pdf(html_content)

        filename = f'mandari-datenexport-{timezone.now().strftime("%Y%m%d")}.pdf'
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _html_to_pdf(self, html_content: str) -> bytes:
        """Convert HTML to PDF using xhtml2pdf with reportlab fallback."""
        try:
            from xhtml2pdf import pisa

            result = io.BytesIO()
            pisa_status = pisa.CreatePDF(
                src=html_content,
                dest=result,
                encoding="UTF-8",
            )

            if pisa_status.err:
                raise Exception(f"PDF generation error: {pisa_status.err}")

            return result.getvalue()

        except ImportError:
            return self._simple_pdf_fallback(html_content)

    def _simple_pdf_fallback(self, html_content: str) -> bytes:
        """Fallback PDF generation using reportlab."""
        import re

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=25 * mm,
            bottomMargin=20 * mm,
        )

        styles = getSampleStyleSheet()
        story = []

        text = re.sub(r"<[^>]+>", "", html_content)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")

        for line in text.split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(line, styles["Normal"]))
                story.append(Spacer(1, 4))

        if story:
            doc.build(story)
        return buffer.getvalue()


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
    """Safely get a decrypted field value via its property."""
    try:
        return getattr(obj, field_name, "") or ""
    except Exception:
        return "[Inhalt konnte nicht entschlüsselt werden]"


# Singleton instance
dsgvo_export_service = DsgvoExportService()
