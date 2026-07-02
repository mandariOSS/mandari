# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erinnert Mitglieder an bald fällige und überfällige Aufgaben.

Gedacht für einen täglichen Cron-Lauf, z. B.:
    python manage.py send_task_due_reminders

Benachrichtigt die zugewiesene Person (Fallback: Ersteller) über Aufgaben,
die morgen oder heute fällig oder bereits überfällig sind. Pro Aufgabe und
Tag wird höchstens eine Erinnerung verschickt (Deduplizierung über die
bereits erzeugten Notifications).
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.tasks.models import Task


class Command(BaseCommand):
    help = "Sendet Erinnerungen für bald fällige und überfällige Aufgaben (täglicher Cron)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-ahead",
            type=int,
            default=1,
            help="Wie viele Tage im Voraus erinnert wird (Default: 1 = morgen fällig).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, was verschickt würde.",
        )

    def handle(self, *args, **options):
        days_ahead = options["days_ahead"]
        dry_run = options["dry_run"]

        today = timezone.now().date()
        horizon = today + timedelta(days=days_ahead)

        due_tasks = (
            Task.objects.filter(
                due_date__lte=horizon,
                status__in=["todo", "in_progress"],
                is_completed=False,
            )
            .select_related("assigned_to__user", "created_by__user", "organization")
            .order_by("due_date")
        )

        sent = skipped = 0
        for task in due_tasks:
            recipient = task.assigned_to or task.created_by
            if not recipient or not recipient.is_active:
                skipped += 1
                continue

            # Dedupe: heute schon an diese Person zu dieser Aufgabe erinnert?
            already_sent_today = Notification.objects.filter(
                recipient=recipient,
                notification_type=NotificationType.TASK_DUE_SOON,
                metadata__task_id=str(task.id),
                created_at__date=today,
            ).exists()
            if already_sent_today:
                skipped += 1
                continue

            days_left = (task.due_date - today).days
            if dry_run:
                self.stdout.write(
                    f"DRY-RUN: '{task.title}' -> {recipient.user.email} (faellig in {days_left} Tagen)"
                )
            else:
                NotificationHub.notify_task_due_soon(task, recipient, days_left)
            sent += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Wuerde senden' if dry_run else 'Gesendet'}: {sent} Erinnerungen, {skipped} uebersprungen."
            )
        )
