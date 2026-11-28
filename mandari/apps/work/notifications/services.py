# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Notification Hub/Service for the Work module.

Central service for sending notifications across the application.
Usage:
    from apps.work.notifications.services import NotificationHub

    # Send to single user
    NotificationHub.send(
        recipient=membership,
        notification_type=NotificationType.TASK_ASSIGNED,
        title="Neue Aufgabe",
        message="Ihnen wurde eine Aufgabe zugewiesen.",
        link="/work/org/tasks/123/",
        actor=assigner_membership,
    )

    # Send to multiple users
    NotificationHub.send_bulk(
        recipients=[member1, member2],
        notification_type=NotificationType.MEETING_REMINDER,
        title="Sitzungserinnerung",
        message="Die Sitzung beginnt in 30 Minuten.",
    )
"""

import logging
import uuid
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from typing import Any, cast

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import (
    ANNOUNCEMENT_WITHDRAWN_KEY,
    TASK_NON_PUBLIC_KEY,
    Notification,
    NotificationPreference,
    NotificationType,
)

logger = logging.getLogger(__name__)


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Kennung aus ``Notification.metadata`` (dort als Text) oder ``None``, wenn sie keine ist."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class NotificationHub:
    """
    Central notification service.

    Handles:
    - Creating in-app notifications
    - Sending email notifications (based on user preferences)
    - Respecting quiet hours
    - Rate limiting (optional)
    """

    @classmethod
    def send(
        cls,
        recipient,  # Membership instance
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,  # Membership instance (optional)
        metadata: dict = None,
        send_email: bool = True,
        _forwarded: bool = False,  # Recursion guard for deputy forwarding
        event_key: str | None = None,
        direct_since: datetime | None = None,
        vertretung_nicht: Collection[Any] = (),
    ) -> Notification | None:
        """
        Send a notification to a single user.

        Args:
            recipient: The Membership to notify
            notification_type: Type of notification (from NotificationType)
            title: Notification title
            message: Notification message
            link: Optional link to related content
            actor: Optional Membership who triggered the notification
            metadata: Optional additional data
            send_email: Whether to send email (subject to user preferences)
            _forwarded: Internal flag to prevent recursive deputy forwarding
            event_key: Kennung des Ereignisses der Datendrehscheibe (Abonnement ``benachrichtigung``):
                höchstens eine Benachrichtigung je Ereignis und Empfänger, Mailauftrag mit demselben Schlüssel
            direct_since: Umschalten (Issue #529): keine Benachrichtigung, wenn der bisherige Weg in der
                Anfrage dieselbe seit diesem Zeitpunkt schon angelegt hat
            vertretung_nicht: Kennungen von Mitgliedschaften, die diese Nachricht nicht als Vertretung bekommen
                (Issue #1512): Sie erhalten sie schon selbst oder sollen sie gar nicht erhalten

        Returns:
            The created Notification instance, or None if filtered out
        """
        # Don't notify yourself
        if actor and actor.id == recipient.id:
            return None

        # Deaktivierte Mitgliedschaften und Konten bekommen nichts mehr, auch nicht als Vertretung: Zuständigkeit
        # und Erstellerbezug bleiben beim Deaktivieren stehen, die Person hat aber keinen Zugang mehr. Eine
        # eingetragene Vertretung erfährt es wie bisher; für sie gelten in ihrem Aufruf dieselben Prüfungen.
        if not recipient.is_active or not recipient.user.is_active:
            if not _forwarded:
                cls._forward_to_deputy(
                    recipient=recipient,
                    notification_type=notification_type,
                    title=title,
                    message=message,
                    link=link,
                    actor=actor,
                    metadata=metadata,
                    event_key=f"{event_key}:vertretung" if event_key else None,
                    direct_since=direct_since,
                    send_email=send_email if event_key else True,
                    vertretung_nicht=vertretung_nicht,
                )
            return None

        # Sitzungsarten (Issue #1239): nichts zu einer Fraktionssitzung, die der Empfänger nicht sehen darf. Gilt für
        # jeden Weg, auch für die Weiterleitung an eine Vertretung.
        if cls._sitzung_verborgen(recipient, notification_type, metadata):
            return None

        # Aufgabe aus einem nichtöffentlichen TOP, die der Empfänger nicht öffnen darf (``Task.noe_gesperrt``): Titel
        # und Kommentarauszug gehören zum nichtöffentlichen Teil der Sitzung, in der Anwendung wie per E-Mail.
        if cls._aufgabe_verschlossen(recipient, metadata):
            return None

        # Typ-Einstellung (Issue #70): In-App-Benachrichtigungen sind je Typ
        # in den Benachrichtigungseinstellungen abschaltbar (Default: an).
        # Der E-Mail-Kanal wird separat in _queue_email geprüft.
        try:
            prefs = NotificationPreference.objects.filter(membership=recipient).first()
            if prefs and not prefs.is_type_enabled(notification_type, "in_app"):
                return None
        except Exception:
            logger.exception("Benachrichtigungseinstellungen konnten nicht geprüft werden")

        # Aus einem Ereignis: einmal je Ereignis und Empfänger (erneute Zustellung legt nichts an)
        key = f"{event_key}:{recipient.id}" if event_key else None
        if key and Notification.objects.filter(event_key=key).exists():
            return None
        if direct_since and cls.already_direct(recipient, notification_type, metadata, direct_since):
            return None

        # Create the notification
        notification = Notification.objects.create(
            recipient=recipient,
            notification_type=notification_type,
            title=title,
            message=message,
            link=link,
            actor=actor,
            metadata=metadata or {},
            event_key=key,
        )

        # Invalidate count cache for recipient
        cls.invalidate_count_cache(recipient)

        # Handle email notification asynchronously
        if send_email:
            cls._queue_email(notification)

        # Erst nach dem Commit und ohne Adresse: Eine im Schattenbetrieb zurückgerollte Benachrichtigung
        # erscheint nicht im Protokoll (Rückrufe eines zurückgerollten Sicherungspunkts verfallen)
        recipient_id, notification_id = recipient.id, notification.id
        transaction.on_commit(
            lambda: logger.info(
                "Benachrichtigung %s (%s) an Mitgliedschaft %s", notification_id, notification_type, recipient_id
            )
        )

        # Deputy forwarding: if recipient is absent and has a deputy
        if not _forwarded:
            cls._forward_to_deputy(
                recipient=recipient,
                notification_type=notification_type,
                title=title,
                message=message,
                link=link,
                actor=actor,
                metadata=metadata,
                event_key=f"{event_key}:vertretung" if event_key else None,
                direct_since=direct_since,
                # Aus einem Ereignis: Mail wie beim Empfänger (im Schatten keine). Sonst wie bisher immer mit
                # Mail – Aufrufer ohne Mail (Einladung, Erinnerung, Gast-Freigabe) versenden ihre Mail selbst
                # an den Empfänger, nicht an die Vertretung.
                send_email=send_email if event_key else True,
                vertretung_nicht=vertretung_nicht,
            )

        return notification

    @staticmethod
    def _sitzung_verborgen(recipient, notification_type: str, metadata: dict | None) -> bool:
        """
        Betrifft die Benachrichtigung eine Fraktionssitzung (Art „Fraktion“ mit ``meeting_id``), die der Empfänger nicht
        sehen darf (Issue #1239)? Ohne Sitzung (unbekannte oder gelöschte Kennung) gibt es nichts zu verbergen.
        """
        from .models import PREFERENCE_CATEGORIES

        meeting_id = (metadata or {}).get("meeting_id")
        if not meeting_id or str(notification_type) not in PREFERENCE_CATEGORIES["faction"]:
            return False
        from django.core.exceptions import ValidationError

        from apps.work.faction.models import FactionMeeting
        from apps.work.faction.sitzungsarten import darf_sehen

        try:
            meeting = FactionMeeting.objects.select_related("meeting_type").filter(pk=meeting_id).first()
        except (ValueError, TypeError, ValidationError):
            return False
        return meeting is not None and not darf_sehen(recipient, meeting)

    @staticmethod
    def _aufgabe_verschlossen(recipient, metadata: dict | None) -> bool:
        """
        Betrifft die Benachrichtigung (``task_id``) eine Aufgabe aus einem nichtöffentlichen TOP, die der Empfänger
        heute nicht öffnen darf? Für Vereidigte und ohne Aufgabenbezug ohne Abfrage. Eine gelöschte Aufgabe verbirgt
        nur, wenn sie beim Löschen zu einem nichtöffentlichen TOP gehörte (``aufgabe_wird_geloescht``).
        """
        task_id = (metadata or {}).get("task_id")
        if not task_id:
            return False
        from django.core.exceptions import ValidationError

        from apps.work.faction.visibility import is_sworn_member
        from apps.work.tasks.models import Task

        if is_sworn_member(recipient):
            return False
        if (metadata or {}).get(TASK_NON_PUBLIC_KEY):
            return True
        try:
            task = (
                Task.objects.filter(pk=task_id, organization_id=recipient.organization_id)
                .select_related("related_faction_agenda_item__parent")
                .first()
            )
        except (ValueError, TypeError, ValidationError):
            return False
        return task is not None and task.noe_gesperrt(recipient)

    @staticmethod
    def _dokument_verschlossen(recipient, metadata: dict | None) -> bool:
        """
        Betrifft die Benachrichtigung (``motion_id``) ein Dokument, das der Empfänger heute nicht öffnen darf? Titel und
        Auszüge stehen im Text. Ohne Dokument (unbekannte oder gelöschte Kennung) gibt es nichts zu verbergen.
        """
        motion_id = (metadata or {}).get("motion_id")
        if not motion_id:
            return False
        from django.core.exceptions import ValidationError

        from apps.work.motions.models import Motion

        try:
            motion = Motion.objects.filter(pk=motion_id, organization_id=recipient.organization_id).first()
        except (ValueError, TypeError, ValidationError):
            return False
        return motion is not None and not motion.can_access(recipient)

    @classmethod
    def bezug_verborgen(cls, recipient, notification_type: str, metadata: Any) -> bool:
        """
        Die Prüfungen aus ``send`` zum Bezug einer Nachricht (Fraktionssitzung, Aufgabe), am heutigen Stand, dazu der
        Zugang zum Dokument (``motion_id``).

        Der Versand der E-Mail läuft nach dem Anlegen in einem eigenen Auftrag und wartet unter Umständen im
        Postausgang. Dazwischen kann die Vereidigung entzogen, der TOP nichtöffentlich gestellt, die Sitzungsart
        geändert oder die Freigabe des Dokuments entzogen sein; eine versandte Mail lässt sich nicht zurückholen.
        """
        metadata = metadata if isinstance(metadata, dict) else None
        return (
            cls._sitzung_verborgen(recipient, notification_type, metadata)
            or cls._aufgabe_verschlossen(recipient, metadata)
            or cls._dokument_verschlossen(recipient, metadata)
        )

    @classmethod
    def aufgabe_wird_geloescht(cls, task: Any) -> int:
        """
        Vor dem Löschen einer Aufgabe aus einem nichtöffentlichen TOP: ihre Nachrichten kennzeichnen.

        Die Nachrichten bleiben gespeichert und nennen Titel und Kommentarauszug. Ohne die Aufgabe ließe sich nicht
        mehr feststellen, dass sie zum nichtöffentlichen Teil gehörte, und die Nachrichten erschienen für nicht
        Vereidigte wieder (``hidden_reference_ids``). Für Vereidigte ändert sich nichts. Liefert die Zahl der
        gekennzeichneten Nachrichten; andere Aufgaben bleiben unberührt.
        """
        if not task.noe_top:
            return 0
        betroffen = list(
            Notification.objects.filter(metadata__has_key="task_id", metadata__task_id=str(task.pk)).exclude(
                metadata__has_key=TASK_NON_PUBLIC_KEY
            )
        )
        for notification in betroffen:
            notification.metadata = {**notification.metadata, TASK_NON_PUBLIC_KEY: True}
            notification.save(update_fields=["metadata"])
        return len(betroffen)

    @classmethod
    def already_direct(cls, recipient, notification_type: str, metadata: dict | None, since: datetime) -> bool:
        """Hat der bisherige Weg (ohne Ereignis) diese Benachrichtigung seit ``since`` schon angelegt?"""
        return Notification.objects.filter(
            recipient=recipient,
            notification_type=notification_type,
            event_key__isnull=True,
            metadata=metadata or {},
            created_at__gte=since,
        ).exists()

    @classmethod
    def _forward_to_deputy(
        cls,
        recipient,
        notification_type,
        title,
        message,
        link,
        actor,
        metadata,
        event_key=None,
        direct_since=None,
        send_email=True,
        vertretung_nicht: Collection[Any] = (),
    ):
        """
        Forward notification to deputy if recipient is currently absent.

        Vertretung ist nur ein aktives Mitglied mit aktivem Konto und ohne Gastzugang; geprüft wird zum Versand,
        weil eine eingetragene Vertretung später deaktiviert oder Gast geworden sein kann. Für den Bezug der
        Nachricht gilt bei ihr dieselbe Regel wie beim ursprünglichen Empfänger (``_deputy_may_see``): Die
        Prüfungen der ``notify_*``-Methoden laufen vor dem ersten ``send`` und kennen die Vertretung nicht.
        """
        try:
            from apps.work.organization.models import MemberAbsence

            today = timezone.now().date()
            active_absence = (
                MemberAbsence.objects.filter(
                    membership=recipient,
                    is_active=True,
                    start_date__lte=today,
                    end_date__gte=today,
                    notify_deputy=True,
                    deputy__is_active=True,
                    deputy__is_guest=False,
                    deputy__user__is_active=True,
                )
                .select_related("deputy")
                .first()
            )

            if (
                active_absence
                and active_absence.deputy
                and active_absence.deputy_id not in vertretung_nicht
                and cls._deputy_may_see(active_absence.deputy, metadata)
            ):
                cls.send(
                    recipient=active_absence.deputy,
                    notification_type=notification_type,
                    title=f"[Vertretung] {title}",
                    message=message,
                    link=link,
                    actor=actor,
                    metadata=metadata,
                    _forwarded=True,
                    event_key=event_key,
                    direct_since=direct_since,
                    send_email=send_email,
                )
        except Exception as e:
            logger.error(f"Failed to forward notification to deputy: {e}")

    @classmethod
    def _deputy_may_see(cls, deputy, metadata: dict | None) -> bool:
        """
        Darf die Vertretung den Bezug der Benachrichtigung sehen? Titel und Auszüge stehen im Text und gehen ihr
        auch per E-Mail zu.

        Nachrichten zu einem Dokument erhält nur, wer es öffnen darf (``can_access``, Issue #529; bisher nur für
        Unterlagen aus „Nichtöffentliche Vorgänge“). Aufgaben zu einer solchen Unterlage oder aus einem
        nichtöffentlichen TOP einer Fraktionssitzung erhält nur, wer den Bezug öffnen darf.
        Einladungen zu eigenen Terminen erhält nur, wer den Termin sehen darf (``termine.sichtbare``).
        Lässt sich der Bezug nicht mehr auflösen, wird nicht weitergeleitet. Alles andere geht wie bisher an die
        Vertretung, auch gewöhnliche private Aufgaben: Die Sperre reicht nicht weiter als beim Empfänger selbst.
        """
        from django.core.exceptions import ValidationError

        from apps.work.faction.visibility import can_view_item_with_parents
        from apps.work.motions.models import Motion
        from apps.work.tasks.models import Task

        metadata = metadata or {}
        org_id = deputy.organization_id
        try:
            if metadata.get("motion_id"):
                # Wie bei Kommentaren und Freigaben: nur an eine Vertretung, die das Dokument selbst öffnen darf
                motion = Motion.objects.filter(pk=metadata["motion_id"], organization_id=org_id).first()
                return motion is not None and motion.can_access(deputy)
            if metadata.get("task_id"):
                task = (
                    Task.objects.filter(pk=metadata["task_id"], organization_id=org_id)
                    .select_related("related_faction_agenda_item__parent", "related_motion")
                    .first()
                )
                if task is None:
                    return False
                item = task.related_faction_agenda_item
                if item is not None and not can_view_item_with_parents(item, deputy):
                    return False
                return task.related_motion is None or not cls._hidden_from(task.related_motion, deputy)
            if metadata.get("termin_id"):
                # Eigene Termine (Issue #1489): nur, wenn die Vertretung den Termin selbst sehen darf
                from apps.work.termine import termine

                return bool(termine.sichtbare(deputy).filter(pk=metadata["termin_id"]).exists())
            if metadata.get("umfrage_id"):
                # Terminumfrage: nur an eine Vertretung, die selbst gefragt ist oder die Umfrage angelegt hat. Wer
                # sie nur sehen darf, weil er alle Termine verwaltet, stimmt nicht mit ab.
                from apps.work.termine import umfrage

                return umfrage.beteiligt(deputy, metadata["umfrage_id"])
        except (ValueError, TypeError, ValidationError):
            return False
        return True

    @classmethod
    def send_bulk(
        cls,
        recipients: list,  # List of Membership instances
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,
        metadata: dict = None,
        send_email: bool = True,
        event_key: str | None = None,
        direct_since: datetime | None = None,
        vertretung_nicht: Collection[Any] = (),
    ) -> list:
        """
        Send notifications to multiple users.

        Args:
            recipients: List of Memberships to notify
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            link: Optional link
            actor: Optional Membership who triggered this
            metadata: Optional additional data
            send_email: Whether to send emails
            vertretung_nicht: Wer die Nachricht nicht als Vertretung bekommt (siehe ``send``); Empfänger der Liste
                erhalten sie dann nur einmal, wenn sie zugleich eine andere Person der Liste vertreten

        Returns:
            List of created Notification instances
        """
        notifications = []

        for recipient in recipients:
            notification = cls.send(
                recipient=recipient,
                notification_type=notification_type,
                title=title,
                message=message,
                link=link,
                actor=actor,
                metadata=metadata,
                send_email=send_email,
                event_key=event_key,
                direct_since=direct_since,
                vertretung_nicht=vertretung_nicht,
            )
            if notification:
                notifications.append(notification)

        return notifications

    @classmethod
    def send_to_organization(
        cls,
        organization,
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,
        metadata: dict = None,
        exclude_actor: bool = True,
    ) -> list:
        """
        Send notification to all members of an organization.

        Args:
            organization: The Organization instance
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            link: Optional link
            actor: Optional Membership who triggered this
            metadata: Optional additional data
            exclude_actor: Whether to exclude the actor from recipients

        Returns:
            List of created Notification instances
        """
        recipients = organization.memberships.filter(is_active=True)

        if exclude_actor and actor:
            recipients = recipients.exclude(id=actor.id)

        return cls.send_bulk(
            recipients=list(recipients),
            notification_type=notification_type,
            title=title,
            message=message,
            link=link,
            actor=actor,
            metadata=metadata,
        )

    @classmethod
    def _queue_email(cls, notification: Notification):
        """
        Queue email for a notification to be sent asynchronously.

        Uses Django 6.0's background tasks for async processing.
        """
        try:
            # Quick check if email is enabled before queueing
            prefs, _ = NotificationPreference.objects.get_or_create(membership=notification.recipient)

            # Skip if email is disabled for this type
            if not prefs.is_type_enabled(notification.notification_type, "email"):
                return

            # Skip if in quiet hours
            if cls._is_quiet_hours(prefs):
                return

            # Skip if not instant digest
            if prefs.email_digest != "instant":
                return

            # Mail-Auftrag (Warteschlange "mail"): mit TASKS_BACKEND=journal im Runner, sonst sofort.
            # Aus einem Ereignis mit Idempotenzschlüssel je Ereignis und Empfänger (Issue #529).
            from apps.events.tasks_backend import enqueue_once
            from apps.work.background_tasks import send_notification_email_task

            if notification.event_key:
                enqueue_once(cast(Any, send_notification_email_task), notification.event_key, str(notification.id))
            else:
                cast(Any, send_notification_email_task).enqueue(str(notification.id))

        except Exception as e:
            logger.error(f"Failed to queue notification email: {e}")

    @classmethod
    def _is_quiet_hours(cls, prefs: NotificationPreference) -> bool:
        """Check if we're currently in quiet hours."""
        if not prefs.quiet_hours_enabled:
            return False

        if not prefs.quiet_hours_start or not prefs.quiet_hours_end:
            return False

        now = timezone.localtime().time()
        start = prefs.quiet_hours_start
        end = prefs.quiet_hours_end

        # Handle overnight quiet hours (e.g., 22:00 - 07:00)
        if start > end:
            return now >= start or now <= end
        return start <= now <= end

    # =========================================================================
    # Convenience methods for common notification types
    # =========================================================================

    @classmethod
    def notify_task_assigned(
        cls,
        task,
        assignee,  # Membership
        assigner,  # Membership
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify user when a task is assigned to them."""
        return cls.send(
            recipient=assignee,
            notification_type=NotificationType.TASK_ASSIGNED,
            title="Neue Aufgabe zugewiesen",
            message=f"Ihnen wurde die Aufgabe „{task.title}“ zugewiesen.",
            link=f"/work/{task.organization.slug}/tasks/?open={task.id}",
            actor=assigner,
            metadata={"task_id": str(task.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_task_comment(
        cls,
        task,
        comment,  # TaskActivity mit activity_type="comment"
        commenter,  # Membership
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify task assignee/creator about a new comment (``commenter`` None: nicht mehr zuzuordnen)."""
        recipients = set()
        commenter_id = commenter.id if commenter is not None else None

        # Alle Zuständigen: die zugewiesene Person und die weiteren (Issue #1462)
        for person in task.zustaendige():
            if person.id != commenter_id:
                recipients.add(person)
        if task.created_by and task.created_by.id != commenter_id:
            recipients.add(task.created_by)

        if not recipients:
            return []

        preview = (comment.content or "").strip()
        if len(preview) > 100:
            preview = preview[:100] + "…"

        return cls.send_bulk(
            recipients=list(recipients),
            notification_type=NotificationType.TASK_COMMENT,
            title="Neuer Kommentar zur Aufgabe",
            message=f'Neuer Kommentar zu "{task.title}": {preview}',
            link=f"/work/{task.organization.slug}/tasks/?open={task.id}",
            actor=commenter,
            metadata={"task_id": str(task.id), "comment_id": str(comment.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_task_completed(
        cls,
        task,
        completer,  # Membership
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify creator and assignee when someone else completes a task (``completer`` None: unbekannt)."""
        recipients = set()
        completer_id = completer.id if completer is not None else None
        if task.created_by and task.created_by.id != completer_id:
            recipients.add(task.created_by)
        # Alle Zuständigen: Wer die gemeinsame Aufgabe nicht selbst abgehakt hat, erfährt es (Issue #1462)
        for person in task.zustaendige():
            if person.id != completer_id:
                recipients.add(person)

        if not recipients:
            return []

        return cls.send_bulk(
            recipients=list(recipients),
            notification_type=NotificationType.TASK_COMPLETED,
            title="Aufgabe erledigt",
            message=f'Die Aufgabe "{task.title}" wurde als erledigt markiert.',
            link=f"/work/{task.organization.slug}/tasks/?open={task.id}",
            actor=completer,
            metadata={"task_id": str(task.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_task_due_soon(
        cls,
        task,
        recipient,  # Membership
        days_left: int,
    ):
        """Remind assignee about an upcoming or overdue due date."""
        if days_left < 0:
            message = f'Die Aufgabe "{task.title}" ist seit {abs(days_left)} Tag(en) überfällig.'
        elif days_left == 0:
            message = f'Die Aufgabe "{task.title}" ist heute fällig.'
        else:
            message = f'Die Aufgabe "{task.title}" ist in {days_left} Tag(en) fällig.'

        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.TASK_DUE_SOON,
            title="Aufgabe bald fällig" if days_left >= 0 else "Aufgabe überfällig",
            message=message,
            link=f"/work/{task.organization.slug}/tasks/?open={task.id}",
            metadata={"task_id": str(task.id), "days_left": days_left},
        )

    @classmethod
    def notify_meeting_reminder(
        cls,
        meeting,  # OParlMeeting
        recipient,  # Membership
        minutes_before: int = 30,
    ):
        """Send meeting reminder notification."""
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MEETING_REMINDER,
            title="Sitzungserinnerung",
            message=f'Die Sitzung "{meeting.get_display_name()}" beginnt in {minutes_before} Minuten.',
            link=f"/insight/termine/{meeting.id}/",
            metadata={
                "meeting_id": str(meeting.id),
                "minutes_before": minutes_before,
            },
        )

    LEVEL_LABELS = {"view": "Lesen", "comment": "Kommentieren", "edit": "Bearbeiten", "admin": "Verwalten"}

    @staticmethod
    def _hidden_from(motion, recipient) -> bool:
        """
        Unterlage aus „Nichtöffentliche Vorgänge“, die die empfangende Person nicht öffnen darf (Issue #873)?
        Dann keine Nachricht – sie enthielte Titel oder Auszüge.
        """
        return motion.is_sworn_in_only() and not motion.can_access(recipient)

    @classmethod
    def notify_document_shared(
        cls,
        motion,
        recipient,
        level: str,
        sharer,
        send_email: bool = True,
        *,
        event_key: str | None = None,
        direct_since: datetime | None = None,
    ):
        """
        Dokument wurde für ein Mitglied oder einen Gast freigegeben (Issue #75).

        Gäste haben keinen Zugriff auf die Benachrichtigungsseite — für sie ist
        die E-Mail der einzige Kanal, deshalb läuft sie über den normalen
        E-Mail-Pfad (Präferenz „sofort“ ist Standard).
        """
        if cls._hidden_from(motion, recipient):
            return None
        label = cls.LEVEL_LABELS.get(level, level)
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MOTION_SHARED,
            title="Dokument für Sie freigegeben",
            message=f"„{motion.title}“ wurde für Sie freigegeben (Stufe: {label}).",
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
            actor=sharer,
            metadata={"motion_id": str(motion.id), "level": level},
            send_email=send_email,
            event_key=event_key,
            direct_since=direct_since,
        )

    @classmethod
    def notify_motion_shared(cls, motion, share, sharer):
        """Kompatibilitäts-Wrapper: Freigabe aus einer MotionShare (scope=user) melden."""
        from apps.tenants.models import Membership

        if share.scope != "user" or not share.user_id:
            return None
        recipient = Membership.objects.filter(user=share.user, organization=motion.organization, is_active=True).first()
        if recipient is None:
            return None
        return cls.notify_document_shared(motion, recipient, share.level, sharer)

    @classmethod
    def notify_folder_shared(cls, folder, recipient, level: str, sharer, send_email: bool = True):
        """Ordner (inkl. Unterordner) wurde für einen Gast freigegeben (Issue #75)."""
        from django.urls import reverse

        label = cls.LEVEL_LABELS.get(level, level)
        slug = folder.organization.slug
        if recipient.is_guest:
            link = reverse("work:guest_documents", kwargs={"org_slug": slug})
        else:
            link = f"/work/{slug}/documents/?ordner={folder.id}"
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MOTION_SHARED,
            title="Ordner für Sie freigegeben",
            message=f"Der Ordner „{folder.name}“ (inkl. Unterordner) wurde für Sie freigegeben (Stufe: {label}).",
            link=link,
            actor=sharer,
            metadata={"folder_id": str(folder.id), "level": level},
            send_email=send_email,
        )

    @classmethod
    def notify_motion_comment(
        cls,
        comment,
        actor,
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """
        Neuer Kommentar: Autor:in des Dokuments und alle bisherigen
        Kommentator:innen informieren (ohne die kommentierende Person selbst).
        Gäste erhalten die Nachricht per E-Mail. ``actor`` ``None``: nicht mehr zuzuordnen.
        """
        from apps.work.motions.models import MotionComment

        motion = comment.motion
        actor_id = actor.id if actor is not None else None
        recipients = {}
        if motion.author_id and motion.author_id != actor_id:
            recipients[motion.author_id] = motion.author
        previous = MotionComment.objects.filter(motion=motion).exclude(id=comment.id).select_related("author__user")
        if actor_id is not None:
            previous = previous.exclude(author_id=actor_id)
        for prev in previous:
            if prev.author is not None:  # Kommentare ehemaliger Mitglieder
                recipients.setdefault(prev.author_id, prev.author)

        excerpt = " ".join((comment.content or "").split())[:140]
        if actor is None:
            actor_name = "Jemand"
        elif hasattr(actor.user, "get_display_name"):
            actor_name = actor.user.get_display_name()
        else:
            actor_name = actor.user.email
        sent = []
        for recipient in recipients.values():
            # Die Liste stammt aus früheren Kommentaren. Wer das Dokument inzwischen nicht mehr öffnen darf
            # (Freigabe entzogen, Dokument privat gestellt, Recht genommen), erfährt weder Titel noch Auszug.
            if not recipient.is_active or not motion.can_access(recipient):
                continue
            notification = cls.send(
                recipient=recipient,
                notification_type=NotificationType.MOTION_COMMENT,
                title="Neuer Kommentar",
                message=f"{actor_name} hat „{motion.title}“ kommentiert: „{excerpt}“",
                link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
                actor=actor,
                metadata={"motion_id": str(motion.id), "comment_id": str(comment.id)},
                event_key=event_key,
                send_email=send_email,
                direct_since=direct_since,
            )
            if notification:
                sent.append(notification)
        return sent

    @classmethod
    def notify_motion_assigned(
        cls,
        motion,
        assignee,  # Membership
        assigner,  # Membership
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify user when they are set as responsible for a document."""
        if cls._hidden_from(motion, assignee):
            return None
        return cls.send(
            recipient=assignee,
            notification_type=NotificationType.MOTION_ASSIGNED,
            title="Federführung übertragen",
            message=f"Ihnen wurde die Federführung für „{motion.title}“ übertragen.",
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
            actor=assigner,
            metadata={"motion_id": str(motion.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_motion_ris_status(cls, motion, recipient, title: str, message: str):
        """Rückmeldung der Verwaltung zu einem eingereichten Antrag (Issue #40)."""
        if cls._hidden_from(motion, recipient):
            return None
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MOTION_STATUS,
            title=title,
            message=message,
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/submit-ris/",
            metadata={"motion_id": str(motion.id), "source": "session"},
        )

    @classmethod
    def notify_motion_due_soon(
        cls,
        motion,
        recipient,  # Membership
        days_left: int,
    ):
        """Remind responsible member about an upcoming or overdue document deadline."""
        if cls._hidden_from(motion, recipient):
            return None
        if days_left < 0:
            message = f'Das Dokument "{motion.title}" ist seit {abs(days_left)} Tag(en) überfällig.'
        elif days_left == 0:
            message = f'Das Dokument "{motion.title}" ist heute fällig.'
        else:
            message = f'Das Dokument "{motion.title}" ist in {days_left} Tag(en) fällig.'

        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MOTION_DUE_SOON,
            title="Dokument bald fällig" if days_left >= 0 else "Dokument überfällig",
            message=message,
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
            metadata={"motion_id": str(motion.id), "days_left": days_left},
        )

    @classmethod
    def notify_motion_approval_requested(
        cls,
        approval,  # MotionApproval
        requester,  # Membership
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify a member that their approval was requested for a document."""
        motion = approval.motion
        if approval.approver is None or cls._hidden_from(motion, approval.approver):
            return None
        return cls.send(
            recipient=approval.approver,
            notification_type=NotificationType.MOTION_APPROVAL_REQUESTED,
            title="Freigabe angefragt",
            message=f"Ihre Freigabe ({approval.get_approval_type_display()}) für „{motion.title}“ wurde angefragt.",
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
            actor=requester,
            metadata={"motion_id": str(motion.id), "approval_id": str(approval.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_motion_approval_decided(
        cls,
        approval,  # MotionApproval
        decider,  # Membership
        recipient,  # Membership (z. B. Autor oder Federführung)
        *,
        event_key: str | None = None,
        send_email: bool = True,
        direct_since: datetime | None = None,
    ):
        """Notify document owner about an approval decision."""
        motion = approval.motion
        # Autor:in und Federführung stehen am Dokument, auch wenn sie es nicht mehr öffnen dürfen (etwa ohne
        # ``motions.view``): dann keine Nachricht mit Titel und Ergebnis
        if not motion.can_access(recipient):
            return None
        decision = "erteilt" if approval.approved else "abgelehnt"
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MOTION_APPROVAL_DECIDED,
            title=f"Freigabe {decision}",
            message=f'Die Freigabe ({approval.get_approval_type_display()}) für "{motion.title}" wurde {decision}.',
            link=f"/work/{motion.organization.slug}/documents/{motion.id}/",
            actor=decider,
            metadata={"motion_id": str(motion.id), "approval_id": str(approval.id)},
            event_key=event_key,
            send_email=send_email,
            direct_since=direct_since,
        )

    @classmethod
    def notify_motion_approval_decision(cls, approval, decider, **optionen: Any) -> list:
        """Entschiedene Freigabe an Autor:in und Federführung melden (ehemalige Mitglieder haben keinen Verweis mehr).

        ``optionen`` gehen an :meth:`notify_motion_approval_decided` (``event_key``, ``send_email``, ``direct_since``).
        """
        motion = approval.motion
        recipients = {}
        if motion.author:
            recipients[motion.author_id] = motion.author
        if motion.responsible:
            recipients[motion.responsible_id] = motion.responsible
        sent = []
        for recipient in recipients.values():
            notification = cls.notify_motion_approval_decided(approval, decider, recipient, **optionen)
            if notification:
                sent.append(notification)
        return sent

    @classmethod
    def notify_member_joined(
        cls,
        organization,
        new_member,  # Membership
        inviter=None,  # Membership (optional)
    ):
        """Mitglieder benachrichtigen, die Zugänge verwalten dürfen (members.invite), wenn jemand beitritt."""
        from apps.work.organization.selectors import registration_reviewers

        excluded = {new_member.id, inviter.id if inviter else None}
        admins = [membership for membership in registration_reviewers(organization) if membership.id not in excluded]

        return cls.send_bulk(
            recipients=admins,
            notification_type=NotificationType.MEMBER_JOINED,
            title="Neues Mitglied",
            message=f"{new_member.user.get_full_name() or new_member.user.email} ist der Organisation beigetreten.",
            link=f"/work/{organization.slug}/organization/members/",
            actor=inviter,
            metadata={"member_id": str(new_member.id)},
        )

    # =========================================================================
    # Support Ticket notifications
    # =========================================================================

    @classmethod
    def notify_support_ticket_created(
        cls,
        ticket,
        creator,  # Membership
    ):
        """
        Notify support staff when a new ticket is created.
        This sends to all users with support.manage permission.
        """
        # For now, just log - staff notifications would go through admin
        logger.info(f"Support ticket created: {ticket.id} by {creator.user.email}")
        return

    @classmethod
    def notify_support_ticket_reply(
        cls,
        ticket,
        message,
        replier=None,  # Membership or None for staff
        is_staff_reply: bool = False,
    ):
        """
        Notify the ticket creator when staff replies.
        """
        if is_staff_reply and ticket.created_by:
            return cls.send(
                recipient=ticket.created_by,
                notification_type=NotificationType.SUPPORT_TICKET_REPLY,
                title="Antwort auf Ihr Support-Ticket",
                message=f'Ihr Ticket "{ticket.subject}" hat eine neue Antwort erhalten.',
                link=f"/work/{ticket.organization.slug}/support/{ticket.id}/",
                metadata={"ticket_id": str(ticket.id), "message_id": str(message.id)},
            )
        return None

    @classmethod
    def notify_support_ticket_status_change(
        cls,
        ticket,
        old_status: str,
        new_status: str,
    ):
        """
        Notify the ticket creator when status changes.
        """
        if not ticket.created_by:
            return None

        status_labels = dict(ticket.STATUS_CHOICES)
        new_status_label = status_labels.get(new_status, new_status)

        notification_type = NotificationType.SUPPORT_TICKET_STATUS
        title = f"Ticketstatus geändert: {new_status_label}"
        message = f'Ihr Ticket "{ticket.subject}" hat jetzt den Status: {new_status_label}'

        # Special cases for resolved/escalated
        if new_status == "resolved":
            notification_type = NotificationType.SUPPORT_TICKET_RESOLVED
            title = "Ihr Support-Ticket wurde gelöst"
            message = f'Ihr Ticket "{ticket.subject}" wurde als gelöst markiert. Falls das Problem weiterhin besteht, können Sie das Ticket wieder öffnen.'
        elif new_status == "escalated":
            notification_type = NotificationType.SUPPORT_TICKET_ESCALATED
            title = "Ihr Ticket wurde eskaliert"
            message = f'Ihr Ticket "{ticket.subject}" wurde an ein höheres Support-Level weitergeleitet.'

        return cls.send(
            recipient=ticket.created_by,
            notification_type=notification_type,
            title=title,
            message=message,
            link=f"/work/{ticket.organization.slug}/support/{ticket.id}/",
            metadata={
                "ticket_id": str(ticket.id),
                "old_status": old_status,
                "new_status": new_status,
            },
        )

    # =========================================================================
    # Utility methods
    # =========================================================================

    @classmethod
    def _get_count_cache_key(cls, membership) -> str:
        """Generate cache key for notification count."""
        return cls._count_cache_key_for_id(membership.id)

    @staticmethod
    def _count_cache_key_for_id(membership_id: Any) -> str:
        return f"notif_count_{membership_id}"

    @classmethod
    def for_recipient(cls, membership: Any) -> QuerySet[Notification]:
        """
        Benachrichtigungen der Person für Glocke und Benachrichtigungszentrale.

        Nachrichten zu Unterlagen in „Nichtöffentliche Vorgänge“, die die Person nicht (mehr) öffnen darf – etwa
        nach Entzug der Vereidigung –, bleiben gespeichert, erscheinen aber nicht (Issue #873). Dasselbe gilt für
        zurückgezogene Ankündigungen (Issue #857) und für Nachrichten, deren Bezug sich für die Person nicht
        (mehr) prüfen lässt oder nichtöffentlich ist (``hidden_reference_ids``).
        """
        from apps.work.motions.non_public import hidden_document_ids

        notifications = Notification.objects.filter(recipient=membership).exclude(
            # Zurückgezogene Ankündigungen bleiben gespeichert, erscheinen aber nicht mehr (Issue #857)
            notification_type=NotificationType.ANNOUNCEMENT,
            metadata__has_key=ANNOUNCEMENT_WITHDRAWN_KEY,
        )
        hidden = hidden_document_ids(membership)
        if hidden:
            # Nur Nachrichten mit ``motion_id``: Ohne diese Bedingung fiele jede Nachricht ohne die Angabe mit
            # heraus (NOT über NULL), also auch Aufgaben, Einladungen und Erwähnungen.
            notifications = notifications.exclude(Q(metadata__has_key="motion_id") & Q(metadata__motion_id__in=hidden))
        unverifiable = cls.hidden_reference_ids(membership)
        if unverifiable:
            notifications = notifications.exclude(pk__in=unverifiable)
        return notifications

    @classmethod
    def hidden_reference_ids(cls, membership: Any) -> list[Any]:
        """
        Kennungen der Benachrichtigungen der Person, deren Text Nichtöffentliches nennen kann und die
        ``hidden_document_ids`` nicht erfasst. Titel und Auszüge stehen im gespeicherten Text; geprüft wird
        deshalb beim Lesen gegen den heutigen Stand, nicht nur beim Anlegen:

        - Erwähnung im Protokoll (``item_id``): Der TOP ist nichtöffentlich, auch nachträglich so gestellt oder
          unter einem nichtöffentlichen TOP, oder es gibt ihn nicht mehr.
        - Nachricht zu einer Unterlage (``motion_id``), die endgültig gelöscht ist: Ob sie in „Nichtöffentliche
          Vorgänge“ lag, lässt sich dann nicht mehr feststellen.
        - Nachricht zu einer Aufgabe (``task_id``) aus einem nichtöffentlichen TOP: Sie ist für nicht Vereidigte
          verschlossen (``Task.noe_gesperrt``), auch wenn der TOP erst später nichtöffentlich wurde oder die
          Vereidigung entzogen ist. Das gilt nach dem Löschen der Aufgabe weiter (``aufgabe_wird_geloescht``
          kennzeichnet ihre Nachrichten); Nachrichten zu anderen gelöschten Aufgaben bleiben sichtbar.

        Für vereidigte Mitglieder leer, ohne Abfrage. Sonst eine Abfrage nach den eigenen Nachrichten mit
        solchem Bezug und, nur wenn es sie gibt, je eine nach den Unterlagen, den TOPs und den Aufgaben.
        """
        from apps.work.faction.models import FactionAgendaItem
        from apps.work.faction.visibility import is_item_internal, is_sworn_member
        from apps.work.motions.models import Motion

        if membership is None or is_sworn_member(membership):
            return []
        rows = list(
            Notification.objects.filter(recipient=membership)
            .filter(
                Q(metadata__has_key="motion_id")
                | Q(metadata__has_key="task_id")
                | Q(notification_type=NotificationType.FACTION_MENTION)
            )
            .values_list("pk", "notification_type", "metadata")
        )
        if not rows:
            return []

        org_id = membership.organization_id
        by_motion: dict[uuid.UUID | None, list[Any]] = {}
        by_item: dict[uuid.UUID | None, list[Any]] = {}
        by_task: dict[uuid.UUID | None, list[Any]] = {}
        geloescht_nichtoeffentlich: set[Any] = set()
        for pk, notification_type, metadata in rows:
            metadata = metadata if isinstance(metadata, dict) else {}
            if notification_type == NotificationType.FACTION_MENTION:
                by_item.setdefault(_as_uuid(metadata.get("item_id")), []).append(pk)
            if "motion_id" in metadata:
                by_motion.setdefault(_as_uuid(metadata.get("motion_id")), []).append(pk)
            if "task_id" in metadata:
                by_task.setdefault(_as_uuid(metadata.get("task_id")), []).append(pk)
                if metadata.get(TASK_NON_PUBLIC_KEY):
                    geloescht_nichtoeffentlich.add(pk)

        hidden: set[Any] = set(geloescht_nichtoeffentlich)
        if by_task:
            from apps.work.tasks.models import Task
            from apps.work.tasks.selectors import noe_top_q

            locked = set(
                Task.objects.filter(pk__in=[k for k in by_task if k is not None], organization_id=org_id)
                .filter(noe_top_q())
                .values_list("pk", flat=True)
            )
            hidden.update(pk for key, pks in by_task.items() if key in locked for pk in pks)
        if by_motion:
            known = set(
                Motion.objects.filter(
                    pk__in=[k for k in by_motion if k is not None], organization_id=org_id
                ).values_list("pk", flat=True)
            )
            hidden.update(pk for key, pks in by_motion.items() if key not in known for pk in pks)
        if by_item:
            items = {
                item.pk: item
                for item in FactionAgendaItem.objects.filter(
                    pk__in=[k for k in by_item if k is not None], meeting__organization_id=org_id
                ).select_related("parent__parent")
            }
            for key, pks in by_item.items():
                item = items.get(key)
                if item is None or is_item_internal(item):
                    hidden.update(pks)
        return list(hidden)

    @classmethod
    def get_unread_count(cls, membership) -> int:
        """Get count of unread notifications for a user (cached for 30 seconds)."""
        cache_key = cls._get_count_cache_key(membership)
        count = cache.get(cache_key)
        if count is None:
            count = cls.for_recipient(membership).filter(is_read=False).count()
            cache.set(cache_key, count, 30)  # 30 seconds TTL
        return count

    @classmethod
    def invalidate_count_cache(cls, membership):
        """Invalidate the notification count cache for a user."""
        cache_key = cls._get_count_cache_key(membership)
        cache.delete(cache_key)

    @classmethod
    def invalidate_count_caches(cls, membership_ids: Iterable[Any]) -> None:
        """Zähler mehrerer Mitgliedschaften verwerfen (Kennungen genügen)."""
        keys = [cls._count_cache_key_for_id(membership_id) for membership_id in set(membership_ids)]
        if keys:
            cache.delete_many(keys)

    @classmethod
    def mark_all_as_read(cls, membership) -> int:
        """Mark all notifications as read for a user."""
        from . import ankuendigung

        unread = Notification.objects.filter(recipient=membership, is_read=False)
        # Ankündigungen gelten für die Person: auch in ihren anderen Organisationen gelesen (Issue #857)
        announcement_keys = list(
            unread.filter(notification_type=NotificationType.ANNOUNCEMENT).values_list(
                f"metadata__{ankuendigung.METADATEN_SCHLUESSEL}", flat=True
            )
        )
        count = unread.update(is_read=True, read_at=timezone.now())
        # Invalidate cache after marking as read
        cls.invalidate_count_cache(membership)
        if announcement_keys:
            ankuendigung.gelesen_fuer_person(membership.user_id, announcement_keys)
        return count

    @classmethod
    def cleanup_old_notifications(cls, days: int = 90) -> int:
        """Delete notifications older than specified days."""
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = Notification.objects.filter(
            created_at__lt=cutoff,
            is_read=True,
        ).delete()
        return deleted
