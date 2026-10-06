# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Services for faction meeting functionality.

Includes:
- FactionMeetingEmailService: Email invitations and reminders
- AgendaProposalService: Handle agenda item proposals from Sachkundige Bürger*innen
- ProtocolApprovalService: Manage protocol approval workflow
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.email import render_email
from apps.common.ical import build_ics_event
from apps.common.pdf import html_to_pdf

logger = logging.getLogger(__name__)

# Erinnerungen: Vorlauf vor Sitzungsbeginn und Lock gegen parallele Läufe
FACTION_REMINDER_WINDOW_HOURS = 48
_REMINDER_LOCK_KEY = "faction:reminder:lock"
_REMINDER_LOCK_TIMEOUT = 10 * 60


def invitation_attendances(meeting, *, update: bool = False):
    """
    Teilnahmen, an die eine Einladung geht (Mail und In-App): Erstversand an alle Eingeladenen,
    Aktualisierung an alle außer Absagen – jeweils nur aktive Mitglieder. Beim Deaktivieren bleiben
    die Teilnahmen an schon angelegten Sitzungen stehen; sie dürfen keine Einladung mehr auslösen.
    """
    attendances = meeting.attendances.filter(membership__isnull=False, membership__is_active=True)
    if update:
        return attendances.exclude(status="declined")
    return attendances.filter(status="invited")


class FactionMeetingEmailService:
    """
    Service for sending faction meeting emails (Issue #59).

    Einladungen auf dem Niveau der Session-Ladungen mit den geteilten
    Bausteinen aus apps/common:
    - ICS-Kalenderanhang (SEQUENCE wird bei Aktualisierungen erhöht)
    - Tagesordnungs-PDF getrennt Ö/NÖ (Nicht-Vereidigte erhalten nur Ö)
    - Deep-Link zur Sitzung + RSVP-Hinweis
    - Aktualisierungs-/Nachladungsversand nach TO-Änderungen
    - Erinnerungen ~48 h vor Sitzungsbeginn (einmalig je Sitzung)
    """

    # -- Bausteine -------------------------------------------------------

    def get_meeting_url(self, meeting) -> str:
        """Deep-Link zur Sitzungsansicht im Work-Portal."""
        base = getattr(settings, "SITE_URL", "").rstrip("/")
        return f"{base}/work/{meeting.organization.slug}/faction/{meeting.id}/"

    def build_meeting_ics(self, meeting, sequence: int | None = None) -> bytes:
        """ICS-Kalenderanhang (gemeinsamer Baustein apps/common/ical.py)."""
        description_parts = []
        if meeting.description:
            description_parts.append(meeting.description)
        if meeting.video_link:
            description_parts.append(f"Video-Link: {meeting.video_link}")
        description_parts.append(f"Sitzung im Work-Portal: {self.get_meeting_url(meeting)}")

        return build_ics_event(
            uid=f"faction-meeting-{meeting.pk}@mandari",
            summary=meeting.title,
            start=meeting.start,
            end=meeting.end,
            description="\n".join(description_parts),
            location=meeting.location or ("Online" if meeting.is_virtual else ""),
            organizer_name=meeting.organization.name,
            sequence=meeting.invitation_sequence if sequence is None else sequence,
        )

    def build_agenda_pdf(self, meeting, *, include_internal: bool) -> bytes:
        """
        Tagesordnungs-PDF (gemeinsamer Baustein apps/common/pdf.py).

        Args:
            include_internal: NÖ-Teil aufnehmen (nur für Vereidigte mit
                Berechtigung für den nicht-öffentlichen Teil)
        """
        public_items = (
            meeting.agenda_items.filter(visibility="public", proposal_status="active", parent__isnull=True)
            .prefetch_related("children")
            .order_by("order", "number")
        )
        internal_items = []
        if include_internal:
            internal_items = (
                meeting.agenda_items.filter(visibility="internal", proposal_status="active", parent__isnull=True)
                .prefetch_related("children")
                .order_by("order", "number")
            )

        from .visibility import visible_children

        # Unterpunkte: im öffentlichen Teil ohne NÖ-Unterpunkte, außer in der internen Fassung
        for item in public_items:
            item.visible_children = visible_children(item, include_internal=include_internal)
        for item in internal_items:
            item.visible_children = visible_children(item, include_internal=True)

        context = {
            "meeting": meeting,
            "organization": meeting.organization,
            "public_agenda_items": public_items,
            "internal_agenda_items": internal_items,
            "include_internal": include_internal,
            "generated_at": timezone.localtime(),
        }
        html = render_to_string("work/faction/pdf/agenda.html", context)
        return html_to_pdf(html)

    def _include_internal(self, membership) -> bool:
        """NÖ-Teil nur für Vereidigte — zentrale Sichtbarkeitsfunktion (Issue #64)."""
        from .visibility import can_view_internal

        return can_view_internal(membership)

    # -- Einladungen -----------------------------------------------------

    def send_invitations(self, meeting, *, update: bool = False, invitation_mode: str = "opt_in") -> int:
        """
        Send invitation emails to all invited members.

        Args:
            update: Aktualisierung/Nachladung nach TO-Änderungen — geht an
                alle Mitglieder, die nicht abgesagt haben (ICS-SEQUENCE
                wurde vom Aufrufer bereits erhöht)
            invitation_mode: Opt-in/Opt-out je Organisation (Issue #62) —
                steuert nur den Hinweistext (im Opt-out-Modus gilt man als
                angemeldet und sagt bei Bedarf ab)

        Returns the count of successfully sent emails.
        """
        attendances = invitation_attendances(meeting, update=update)

        # Gastzugänge (nur freigegebene Dokumente) erhalten keine Einladungen zu Fraktionssitzungen
        attendances = attendances.exclude(membership__is_guest=True).select_related("membership__user")

        # PDF-Varianten nur einmal erzeugen (Ö-only und vollständig) + ICS
        pdf_public = self.build_agenda_pdf(meeting, include_internal=False)
        pdf_full = self.build_agenda_pdf(meeting, include_internal=True)
        ics_bytes = self.build_meeting_ics(meeting)

        sent_count = 0
        for attendance in attendances:
            if self._send_invitation_email(
                meeting,
                attendance,
                pdf_public=pdf_public,
                pdf_full=pdf_full,
                ics_bytes=ics_bytes,
                update=update,
                invitation_mode=invitation_mode,
            ):
                sent_count += 1

        return sent_count

    def _send_invitation_email(
        self, meeting, attendance, *, pdf_public, pdf_full, ics_bytes, update=False, invitation_mode="opt_in"
    ) -> bool:
        """Send a single invitation email (mit ICS- und Tagesordnungs-Anhang)."""
        user = attendance.membership.user
        if not user.email:
            logger.warning(f"Skipping invitation for user {user.id} - no email address")
            return False

        # NÖ-Teil nur für Vereidigte mit entsprechender Berechtigung
        include_internal = self._include_internal(attendance.membership)
        public_items = meeting.agenda_items.filter(visibility="public", proposal_status="active").order_by(
            "order", "number"
        )

        internal_items = []
        if include_internal:
            internal_items = meeting.agenda_items.filter(visibility="internal", proposal_status="active").order_by(
                "order", "number"
            )

        meeting_url = self.get_meeting_url(meeting)
        context = {
            "meeting": meeting,
            "user": user,
            "organization": meeting.organization,
            "attendance": attendance,
            "public_agenda_items": public_items,
            "internal_agenda_items": internal_items,
            "is_sworn_in": include_internal,
            "is_update": update,
            "invitation_mode": invitation_mode,
            "is_opt_out": invitation_mode == "opt_out",
            # Zu- und Absagen nur, wenn für die Sitzung eingeschaltet (Issue #871)
            "rsvp_enabled": meeting.rsvp_enabled,
            "meeting_url": meeting_url,
        }

        subject = f"Aktualisierte Einladung: {meeting.title}" if update else f"Einladung: {meeting.title}"

        try:
            html_content, text_content = render_email("work/faction/email/invitation.html", context)
        except Exception as e:
            logger.error(f"Failed to render email template: {e}")
            # Fall back to simple text
            html_content = None
            text_content = self._get_simple_invitation_text(
                meeting,
                user,
                public_items,
                internal_items,
                meeting_url=meeting_url,
                update=update,
                opt_out=invitation_mode == "opt_out",
                rsvp=meeting.rsvp_enabled,
            )

        try:
            # Versand über den Weg der Organisation – eigenes SMTP oder mandari-Standard (Issue #65);
            # Tagesordnung (ggf. mit NÖ-Teil) liegt bis zum Versand mit ihrem Schlüssel verschlüsselt
            from apps.common import mail

            mail.send(
                kind="work.fraktion.einladung",
                organization=meeting.organization,
                subject=subject,
                body=text_content,
                html_body=html_content,
                to=[user.email],
                attachments=[
                    ("tagesordnung.pdf", pdf_full if include_internal else pdf_public, "application/pdf"),
                    ("sitzung.ics", ics_bytes, "text/calendar"),
                ],
                fail_silently=False,
            )
            logger.info(f"Invitation sent to {user.email} for meeting {meeting.id} (update={update})")
            return True
        except Exception as e:
            logger.error(f"Failed to send invitation to {user.email}: {e}")
            return False

    def _get_simple_invitation_text(
        self,
        meeting,
        user,
        public_items=None,
        internal_items=None,
        meeting_url="",
        update=False,
        opt_out=False,
        rsvp=False,
    ) -> str:
        """Generate simple text fallback for invitation email."""
        intro = (
            "die Tagesordnung der folgenden Fraktionssitzung wurde aktualisiert:"
            if update
            else "du bist zur folgenden Fraktionssitzung eingeladen:"
        )
        # Ortszeit und deutsche Wochentags-/Monatsnamen – unabhängig von der gerade aktiven Sprache
        from django.utils import translation
        from django.utils.formats import date_format

        local_start = timezone.localtime(meeting.start)
        with translation.override("de"):
            datum = date_format(local_start, "l, d. F Y")
        lines = [
            f"Hallo {user.first_name or user.email},",
            "",
            intro,
            "",
            f"{meeting.title}",
            f"Datum: {datum}",
            f"Uhrzeit: {local_start:%H:%M} Uhr",
        ]

        if meeting.location:
            lines.append(f"Ort: {meeting.location}")

        if meeting.video_link:
            lines.append(f"Video-Link: {meeting.video_link}")

        if meeting_url:
            lines.append(f"Sitzung im Work-Portal: {meeting_url}")

        # Add agenda items
        if public_items:
            lines.extend(["", "TAGESORDNUNG", ""])
            for item in public_items:
                lines.append(f"TOP {item.number}: {item.title}")

        if internal_items:
            lines.extend(["", "NICHT-ÖFFENTLICHER TEIL", ""])
            for item in internal_items:
                lines.append(f"TOP {item.number}: {item.title}")

        if rsvp:
            rsvp_line = (
                "Du bist angemeldet. Falls du nicht teilnehmen kannst, sage bitte in der Sitzungsansicht ab (Absagen)."
                if opt_out
                else "Bitte sage direkt in der Sitzungsansicht zu oder ab (Zusagen/Absagen)."
            )
            lines.extend(["", rsvp_line])
        lines.extend(
            [
                "",
                "Viele Grüße,",
                f"{meeting.organization.name}",
            ]
        )

        return "\n".join(lines)

    # -- Erinnerungen ----------------------------------------------------

    def send_reminder(self, meeting, hours_before: int = 24) -> int:
        """
        Erinnerung verschicken: mit Zu- und Absagen an Zusagen und Vielleicht-Antworten, ohne sie
        (Issue #871) an alle eingeladenen Mitglieder außer Gästen und Absagen.

        Returns the count of successfully sent emails.
        """
        # Nur aktive Mitglieder: Teilnahmen an schon angelegten Sitzungen bleiben beim Deaktivieren stehen
        active = meeting.attendances.filter(membership__isnull=False, membership__is_active=True)
        if meeting.rsvp_enabled:
            recipients = active.filter(status__in=["confirmed", "tentative"])
        else:
            recipients = active.filter(membership__is_guest=False).exclude(status__in=["declined", "absent", "excused"])
        attendances = list(recipients.select_related("membership__user"))
        sent_count = 0

        for attendance in attendances:
            if self._send_reminder_email(meeting, attendance, hours_before):
                sent_count += 1

        # In-App-Erinnerung (Issue #70) — E-Mail ging bereits separat raus
        self._notify_reminder(meeting, attendances, hours_before)

        return sent_count

    def _notify_reminder(self, meeting, attendances, hours_before: int) -> None:
        """In-App-Benachrichtigung zur Sitzungserinnerung (Issue #70)."""
        try:
            from apps.work.notifications.models import NotificationType
            from apps.work.notifications.services import NotificationHub

            recipients = [a.membership for a in attendances]
            if not recipients:
                return

            local_start = timezone.localtime(meeting.start)
            NotificationHub.send_bulk(
                recipients=recipients,
                notification_type=NotificationType.FACTION_MEETING_REMINDER,
                title="Erinnerung: Fraktionssitzung",
                message=(
                    f'"{meeting.title}" beginnt in etwa {hours_before} Stunden '
                    f"({local_start.strftime('%d.%m.%Y %H:%M')} Uhr)."
                ),
                link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
                metadata={"meeting_id": str(meeting.id), "hours_before": hours_before},
                send_email=False,
            )
        except Exception:
            logger.exception("In-App-Erinnerung fehlgeschlagen (meeting=%s)", meeting.id)

    def _send_reminder_email(self, meeting, attendance, hours_before: int) -> bool:
        """Send a single reminder email."""
        user = attendance.membership.user
        if not user.email:
            return False

        context = {
            "meeting": meeting,
            "user": user,
            "organization": meeting.organization,
            "attendance": attendance,
            "hours_before": hours_before,
            "meeting_url": self.get_meeting_url(meeting),
        }

        subject = f"Erinnerung: {meeting.title} in {hours_before} Stunden"

        try:
            html_content, text_content = render_email("work/faction/email/reminder.html", context)
        except Exception:
            # Fall back to simple text
            html_content = None
            text_content = f"Erinnerung: {meeting.title} findet in {hours_before} Stunden statt."

        try:
            # Versand über den Weg der Organisation (Issue #65)
            from apps.common import mail

            mail.send(
                kind="work.fraktion.erinnerung",
                organization=meeting.organization,
                subject=subject,
                body=text_content,
                html_body=html_content,
                to=[user.email],
                fail_silently=False,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send reminder to {user.email}: {e}")
            return False


def run_faction_reminder_pass(now=None) -> dict:
    """
    Periodischer Erinnerungslauf (Issue #59).

    Wird vom Zeitplan ``fraktionserinnerungen_senden`` (apps/work/schedules.py) aufgerufen —
    analog zum Auto-Georef-Lauf. Verschickt einmalig je Sitzung eine
    Erinnerung an Zusagen/Vielleicht-Antworten, sobald der Sitzungsbeginn
    weniger als FACTION_REMINDER_WINDOW_HOURS (48 h) entfernt ist.

    Ein Cache-Lock verhindert parallele Läufe (mehrere Worker/Prozesse).

    Returns:
        Statistik-Dict (meetings, sent bzw. skipped-Grund).
    """
    from django.core.cache import cache

    from .models import FactionMeeting

    now = now or timezone.now()

    if not cache.add(_REMINDER_LOCK_KEY, "1", timeout=_REMINDER_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        stats = {"meetings": 0, "sent": 0}
        window_end = now + timedelta(hours=FACTION_REMINDER_WINDOW_HOURS)
        meetings = FactionMeeting.objects.filter(
            status__in=["planned", "invited"],
            invitation_sent=True,
            reminder_sent_at__isnull=True,
            start__gt=now,
            start__lte=window_end,
        ).select_related("organization")

        service = FactionMeetingEmailService()
        for meeting in meetings:
            hours_before = max(1, int((meeting.start - now).total_seconds() // 3600))
            try:
                sent = service.send_reminder(meeting, hours_before=hours_before)
            except Exception:
                logger.exception("Erinnerungsversand fehlgeschlagen (meeting=%s)", meeting.id)
                continue
            # Einmalig je Sitzung — auch bei 0 Empfängern nicht erneut versuchen
            meeting.reminder_sent_at = now
            meeting.save(update_fields=["reminder_sent_at"])
            stats["meetings"] += 1
            stats["sent"] += sent

        if stats["meetings"]:
            logger.info(
                "Fraktions-Erinnerungen: %d Sitzung(en), %d E-Mail(s) versendet",
                stats["meetings"],
                stats["sent"],
            )
        return stats
    finally:
        cache.delete(_REMINDER_LOCK_KEY)


def org_member(organization, raw_id):
    """Aktive Mitgliedschaft (kein Gastzugang) der Organisation zu einer Formular-ID, sonst ``None``."""
    import uuid

    from apps.tenants.models import Membership

    try:
        member_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return Membership.objects.filter(id=member_id, organization=organization, is_active=True, is_guest=False).first()


def discard_open_invitations(membership) -> int:
    """
    Teilnahmen eines Mitglieds an noch nicht begonnenen Sitzungen löschen (Issue #420).

    Wird beim Entfernen der Mitgliedschaft aufgerufen: Einladungen und Zusagen für künftige
    Sitzungen sind persönlich und gegenstandslos. Teilnahmen an bereits begonnenen Sitzungen
    gehören zur Anwesenheitsliste der Sitzung und bleiben mit geleertem Verweis erhalten.
    Liefert die Anzahl gelöschter Teilnahmen.
    """
    from .models import FactionAttendance

    count, _ = FactionAttendance.objects.filter(membership=membership, meeting__start__gt=timezone.now()).delete()
    return count


def preserve_member_names(membership) -> int:
    """
    Namen eines Mitglieds an Anwesenheit und Protokolleinträgen sichern (Issue #591).

    Wird vor dem Löschen der Mitgliedschaft aufgerufen: Danach leert ``SET_NULL`` die Verweise,
    Protokolle, Anwesenheitslisten und Teilnahmebestätigungen zeigen den hier gesicherten Namen.
    Gilt unabhängig von der Genehmigung des Protokolls; der Eintrag selbst bleibt unverändert
    (``update()`` ohne Sperrprüfung und ohne Änderungshistorie, nur das Namensfeld).
    Liefert die Anzahl aktualisierter Einträge.
    """
    from apps.common.formatting import member_name_snapshot

    from .models import FactionAttendance, FactionProtocolEntry

    name = member_name_snapshot(membership)
    count = 0
    for model in (FactionAttendance, FactionProtocolEntry):
        for fk, snapshot_field in model.MEMBER_NAME_SNAPSHOTS.items():
            count += model.objects.filter(**{fk: membership}).update(**{snapshot_field: name})
    return count


def safe_link_url(url: str) -> bool:
    """Verweise nur als http(s)-Adresse (kein ``javascript:``, ``data:`` u. Ä.)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def visible_item_tasks(item, membership):
    """Aufgaben am TOP im Rahmen der Aufgaben-Sichtbarkeit (private Aufgaben anderer bleiben verborgen)."""
    from apps.work.tasks.selectors import visible_tasks

    base = item.tasks.select_related("assigned_to__user", "created_by__user")
    return visible_tasks(item.meeting.organization, membership, base=base)


def visible_linked_motions(item, membership):
    """Mit dem TOP verknüpfte Dokumente, soweit das Mitglied sie sehen darf (Motion.visible_to)."""
    from apps.work.motions.models import Motion

    return Motion.visible_to(membership).filter(id__in=item.related_motions.values("id"))


def _decorate_protocol_items(items, entries_by_item, *, include_internal: bool):
    """TOPs für das Niederschrift-PDF mit Einträgen/Beschlüssen anreichern (NÖ-Unterpunkte nur intern)."""
    from .visibility import is_item_internal

    decorated = []
    for item in items:
        item.entries_list = entries_by_item.get(item.id, [])
        item.decision_obj = item.recorded_decision
        item.children_list = []
        for child in item.children.all().order_by("order", "number"):
            if not include_internal and is_item_internal(child):
                continue
            child.entries_list = entries_by_item.get(child.id, [])
            child.decision_obj = child.recorded_decision
            item.children_list.append(child)
        decorated.append(item)
    return decorated


def build_faction_protocol_pdf(meeting, *, internal: bool) -> bytes:
    """
    Niederschrift-PDF für eine Fraktionssitzung erzeugen (Issue #60).

    Args:
        meeting: die FactionMeeting
        internal: True = interne Fassung (inkl. NÖ-Teil und TOP-loser
                  Einträge), False = öffentliche Fassung (ausschließlich
                  Ö-TOPs und deren Einträge — NÖ- und TOP-lose Inhalte
                  erscheinen niemals)

    Returns:
        bytes: PDF-Inhalt
    """
    public_items = list(
        meeting.agenda_items.filter(visibility="public", proposal_status="active", parent__isnull=True)
        .prefetch_related("children")
        .order_by("order", "number")
    )
    internal_items = []
    if internal:
        internal_items = list(
            meeting.agenda_items.filter(visibility="internal", proposal_status="active", parent__isnull=True)
            .prefetch_related("children")
            .order_by("order", "number")
        )

    # Protokolleinträge entschlüsseln und je TOP gruppieren
    entries_by_item: dict = {}
    general_entries = []
    for entry in meeting.protocol_entries.select_related(
        "speaker__user", "action_assignee__user", "agenda_item"
    ).order_by("order", "created_at"):
        payload = {
            "type_display": entry.get_entry_type_display(),
            "entry_type": entry.entry_type,
            "content": entry.get_content_decrypted() or "",
            "speaker": entry.speaker_name,
            "assignee": entry.action_assignee_name if entry.entry_type == "action" else "",
        }
        if entry.agenda_item_id:
            entries_by_item.setdefault(entry.agenda_item_id, []).append(payload)
        else:
            # TOP-lose Einträge: ausschließlich in der internen Fassung
            general_entries.append(payload)

    # Teilnehmerverzeichnis aus der Anwesenheitserfassung
    attendances = list(meeting.attendances.select_related("membership__user"))
    participants = {
        "present": [a for a in attendances if a.status == "present"],
        "excused": [a for a in attendances if a.status in ("excused", "declined")],
        "absent": [a for a in attendances if a.status == "absent"],
        "other": [a for a in attendances if a.status not in ("present", "excused", "declined", "absent")],
        "all": attendances,
    }

    context = {
        "meeting": meeting,
        "organization": meeting.organization,
        "internal": internal,
        "variant_label": "Interne Fassung (inkl. nichtöffentlicher Teil)" if internal else "Öffentliche Fassung",
        "agenda_public": _decorate_protocol_items(public_items, entries_by_item, include_internal=internal),
        "agenda_internal": (
            _decorate_protocol_items(internal_items, entries_by_item, include_internal=True) if internal else []
        ),
        "general_entries": general_entries if internal else [],
        "participants": participants,
        "generated_at": timezone.localtime(),
    }
    html = render_to_string("work/faction/pdf/protocol.html", context)
    return html_to_pdf(html)


class AgendaProposalService:
    """
    Service for handling agenda item proposals from Sachkundige Bürger*innen.

    Allows members with 'agenda.propose' permission to suggest agenda items
    for upcoming meetings. These proposals must be reviewed and approved
    by members with 'agenda.approve' permission (Vorsitz, Stellvertretung,
    Geschäftsführung; Issue #872). Erst angenommene Vorschläge stehen mit
    Nummer auf der Tagesordnung.
    """

    @classmethod
    def create_proposal(
        cls,
        meeting,
        title: str,
        description: str,
        proposed_by,
        visibility: str = "public",
    ):
        """
        Create a new agenda item proposal.

        Args:
            meeting: The FactionMeeting to add the proposal to
            title: Title of the proposed agenda item
            description: Description/content of the proposal
            proposed_by: Membership of the person proposing
            visibility: 'public' or 'internal'

        Returns:
            The created FactionAgendaItem in 'proposed' status
        """
        from .models import FactionAgendaItem

        item = FactionAgendaItem(
            meeting=meeting,
            title=title,
            visibility=visibility,
            proposal_status="proposed",
            proposed_by=proposed_by,
            proposed_at=timezone.now(),
            order=9999,  # Will be reordered when accepted
        )
        item.set_description_encrypted(description)
        item.save()

        logger.info(f"Agenda proposal created: '{title}' for meeting {meeting.id} by {proposed_by.user.email}")

        # Notify meeting managers
        cls._notify_managers(meeting, item, proposed_by)

        return item

    @classmethod
    def approvers(cls, meeting, item):
        """
        Wer über den Vorschlag entscheidet: aktive Mitglieder mit ``agenda.approve`` (Issue #872).

        NÖ strikt (Issue #64): Vorschläge für den nicht-öffentlichen Teil sehen nur Vereidigte.
        Gastzugänge haben keine Rechte (``PermissionChecker``).
        """
        from apps.common.permissions import PermissionChecker

        from .visibility import can_view_item

        approvers = []
        memberships = meeting.organization.memberships.filter(is_active=True, is_guest=False).prefetch_related(
            "roles__permissions", "individual_permissions", "denied_permissions"
        )
        for membership in memberships:
            if PermissionChecker(membership).can_approve_agenda_items() and can_view_item(item, membership):
                approvers.append(membership)
        return approvers

    @staticmethod
    def _meeting_date(meeting) -> str:
        return timezone.localtime(meeting.start).strftime("%d.%m.%Y")

    @classmethod
    def _notify_managers(cls, meeting, item, proposed_by):
        """Vorsitz, Stellvertretung und Geschäftsführung (``agenda.approve``) über den Vorschlag informieren."""
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        managers = [m for m in cls.approvers(meeting, item) if m.id != proposed_by.id]
        if not managers:
            return

        if item.visibility == "internal":
            # NÖ strikt (Issue #64): Benachrichtigungen samt E-Mail gehen bei Abwesenheit auch an die Vertretung,
            # die nicht vereidigt sein muss. Titel und Inhalt stehen daher nur in der Sitzung.
            title = "Neuer nicht-öffentlicher TOP-Vorschlag"
            message = (
                f"Für die Sitzung am {cls._meeting_date(meeting)} liegt ein neuer nicht-öffentlicher TOP-Vorschlag "
                "vor. Details stehen nur in der Sitzung."
            )
        else:
            title = "Neuer TOP-Vorschlag"
            message = f'{proposed_by.user.get_display_name()} hat einen TOP vorgeschlagen: "{item.title}"'

        NotificationHub.send_bulk(
            recipients=managers,
            notification_type=NotificationType.FACTION_MEETING_UPDATED,
            title=title,
            message=message,
            link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
            actor=proposed_by,
            metadata={"meeting_id": str(meeting.id), "item_id": str(item.id)},
        )

    @classmethod
    def _notify_decision(cls, item, reviewed_by, *, accepted: bool, reason: str = "") -> None:
        """
        Vorschlagende Person über die Entscheidung informieren (eigener Typ, je Einstellung abschaltbar, Issue #70).

        Nicht-öffentliche Vorschläge ohne Titel und Begründung (NÖ strikt, Issue #64): Die Benachrichtigung geht bei
        Abwesenheit auch an die Vertretung; die Begründung steht in der Sitzung bei den eigenen Vorschlägen.
        """
        if not item.proposed_by:
            return
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        meeting = item.meeting
        outcome = "angenommen" if accepted else "nicht angenommen"
        if item.visibility == "internal":
            message = (
                f"Ihr nicht-öffentlicher TOP-Vorschlag für die Sitzung am {cls._meeting_date(meeting)} wurde "
                f"{outcome}. Details stehen nur in der Sitzung."
            )
        else:
            message = f'Ihr Vorschlag "{item.title}" wurde {outcome}.'
            if reason:
                message += f" Grund: {reason}"

        NotificationHub.send(
            recipient=item.proposed_by,
            notification_type=NotificationType.FACTION_PROPOSAL_DECIDED,
            title="TOP-Vorschlag angenommen" if accepted else "TOP-Vorschlag abgelehnt",
            message=message,
            link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
            actor=reviewed_by,
            metadata={
                "meeting_id": str(item.meeting_id),
                "item_id": str(item.id),
                "decision": "accepted" if accepted else "rejected",
            },
        )

    @classmethod
    def accept_proposal(cls, item, reviewed_by, assign_number: str = None):
        """
        Accept a proposed agenda item.

        Args:
            item: The FactionAgendaItem to accept
            reviewed_by: Membership of the reviewer
            assign_number: Optional TOP number to assign

        Returns:
            True if accepted, False if already processed
        """
        from django.db import transaction

        from . import agenda
        from .models import FactionAgendaItem

        with transaction.atomic():
            # Sitzung und Vorschlag sperren: gleichzeitige Annahmen erhalten verschiedene Nummern, ein doppelter
            # Klick entscheidet (und benachrichtigt) nur einmal (Issue #872)
            agenda.lock_meeting(item.meeting)
            current = (
                FactionAgendaItem.objects.select_for_update().filter(pk=item.pk, proposal_status="proposed").first()
            )
            if current is None:
                return False

            # Nummer und Platz am Ende des jeweiligen Teils — ermittelt, solange der Vorschlag noch nicht mitzählt
            number = assign_number or agenda.next_number(item.meeting, current.visibility)
            order = agenda.next_order(item.meeting)

            if not current.accept_proposal(reviewed_by):
                return False
            current.number = number
            current.order = order
            current.save(update_fields=["number", "order"])
            if not assign_number:
                # Gleicht Altbestand aus, in dem offene oder abgelehnte Vorschläge noch mitgezählt wurden
                agenda.renumber(item.meeting, current.visibility)

        item.refresh_from_db()
        logger.info(f"Agenda proposal accepted: '{item.title}' by {reviewed_by.user.email}")
        cls._notify_decision(item, reviewed_by, accepted=True)
        return True

    @classmethod
    def reject_proposal(cls, item, reviewed_by, reason: str = ""):
        """
        Reject a proposed agenda item.

        Args:
            item: The FactionAgendaItem to reject
            reviewed_by: Membership of the reviewer
            reason: Reason for rejection

        Returns:
            True if rejected, False if already processed
        """
        from django.db import transaction

        from .models import FactionAgendaItem

        with transaction.atomic():
            # Doppelter Klick oder zwei Genehmigende gleichzeitig: nur eine Entscheidung und eine Benachrichtigung
            current = (
                FactionAgendaItem.objects.select_for_update().filter(pk=item.pk, proposal_status="proposed").first()
            )
            if current is None or not current.reject_proposal(reviewed_by, reason):
                return False

        item.refresh_from_db()
        logger.info(f"Agenda proposal rejected: '{item.title}' by {reviewed_by.user.email}")
        cls._notify_decision(item, reviewed_by, accepted=False, reason=reason)
        return True

    @classmethod
    def get_pending_proposals(cls, meeting):
        """Get all pending proposals for a meeting."""
        return meeting.agenda_items.filter(proposal_status="proposed")

    @classmethod
    def get_proposals_by_member(cls, membership):
        """Get all proposals by a specific member across all meetings."""
        from .models import FactionAgendaItem

        return (
            FactionAgendaItem.objects.filter(proposed_by=membership).select_related("meeting").order_by("-proposed_at")
        )


class ProtocolApprovalService:
    """
    Service for managing protocol approval workflow.

    The workflow:
    1. Meeting ends -> Protocol status is 'draft'
    2. Protocol is submitted for approval -> Status becomes 'pending'
    3. In the next meeting, the approval agenda item is voted on
    4. If approved -> Previous meeting's protocol status becomes 'approved'
    """

    @classmethod
    def submit_for_approval(cls, meeting):
        """
        Submit a meeting's protocol for approval in the next meeting.

        Args:
            meeting: The FactionMeeting whose protocol is ready

        Returns:
            True if submitted, False if already approved
        """
        if not meeting.submit_protocol_for_approval():
            return False

        logger.info(f"Protocol submitted for approval: meeting {meeting.id}")
        return True

    @classmethod
    def approve_protocol(cls, meeting, approved_in_meeting, approved_by):
        """
        Approve a meeting's protocol.

        Args:
            meeting: The FactionMeeting whose protocol is being approved
            approved_in_meeting: The meeting where approval is happening
                (None bei direkter Genehmigung über die Sitzungsansicht)
            approved_by: Membership who approved

        Returns:
            True if approved, False if already approved
        """
        if meeting.protocol_approved:
            return False

        meeting.protocol_status = "approved"
        meeting.protocol_approved = True
        meeting.protocol_approved_at = timezone.now()
        meeting.protocol_approved_by = approved_by
        meeting.protocol_approved_in = approved_in_meeting
        meeting.save(
            update_fields=[
                "protocol_status",
                "protocol_approved",
                "protocol_approved_at",
                "protocol_approved_by",
                "protocol_approved_in",
            ]
        )

        logger.info(
            f"Protocol approved: meeting {meeting.id} "
            f"approved in meeting {approved_in_meeting.id if approved_in_meeting else 'direct'} "
            f"by {approved_by.user.email}"
        )

        # In-App-Benachrichtigung an die Mitglieder (Issue #70) — nur der
        # Sitzungstitel, keine Protokollinhalte
        cls._notify_protocol_approved(meeting, approved_by)

        return True

    @classmethod
    def _notify_protocol_approved(cls, meeting, approved_by):
        """Mitglieder über die Protokoll-Genehmigung informieren (Issue #70)."""
        try:
            from apps.work.notifications.models import NotificationType
            from apps.work.notifications.services import NotificationHub

            recipients = list(
                meeting.organization.memberships.filter(is_active=True, is_guest=False)
                .exclude(id=approved_by.id)
                .select_related("user")
            )
            if not recipients:
                return

            NotificationHub.send_bulk(
                recipients=recipients,
                notification_type=NotificationType.FACTION_PROTOCOL_APPROVED,
                title="Protokoll genehmigt",
                message=f'Das Protokoll der Sitzung "{meeting.title}" wurde genehmigt.',
                link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
                actor=approved_by,
                metadata={"meeting_id": str(meeting.id)},
                send_email=False,
            )
        except Exception:
            logger.exception("Benachrichtigung zur Protokoll-Genehmigung fehlgeschlagen (meeting=%s)", meeting.id)

    @classmethod
    def get_pending_approvals(cls, organization):
        """
        Get all meetings with protocols pending approval.

        Args:
            organization: The Organization to filter by

        Returns:
            QuerySet of FactionMeeting with pending protocol status
        """
        from .models import FactionMeeting

        return FactionMeeting.objects.filter(
            organization=organization,
            protocol_status="pending",
        ).order_by("-start")

    @classmethod
    def auto_create_approval_item(cls, meeting):
        """
        Automatically create the protocol approval agenda item.

        Called when a new meeting is created to add the standard
        first agenda item for approving the previous meeting's protocol.

        Args:
            meeting: The new FactionMeeting

        Returns:
            The created FactionAgendaItem or None if no previous meeting
        """
        if not meeting.previous_meeting:
            # No previous meeting to approve
            return meeting.create_approval_agenda_item()

        # Ensure previous meeting's protocol is in pending status
        prev = meeting.previous_meeting
        if prev.protocol_status == "draft" and prev.status == "completed":
            prev.submit_protocol_for_approval()

        return meeting.create_approval_agenda_item()
