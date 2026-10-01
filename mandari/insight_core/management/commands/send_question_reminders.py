# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Ratsmitglieder an offene Ratsfragen erinnern.

Cronjob (täglich):
    python manage.py send_question_reminders
Erste Erinnerung nach 14 Tagen ohne Antwort, danach alle 14 Tage. Solange die Ratsfragen pausiert
sind (``INSIGHT_QUESTIONS_ENABLED``, Standard aus, Issue #734), endet der Befehl mit einem Hinweis und
ohne Wirkung; der Cron-Eintrag kann bleiben.
"""

from django.core.management.base import BaseCommand

from apps.common.einmalig import EinmaligMixin


class Command(EinmaligMixin, BaseCommand):
    sperre = "send_question_reminders"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
    help = "Erinnert Ratsmitglieder per E-Mail an unbeantwortete öffentliche Fragen"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Tage nach Veröffentlichung bis zur ersten Erinnerung")
        parser.add_argument("--repeat", type=int, default=14, help="Abstand zwischen weiteren Erinnerungen (Tage)")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nicht senden")

    def handle(self, *args, **options):
        from insight_core.services.question_service import questions_enabled, send_due_reminders

        if not questions_enabled():
            self.stdout.write(
                self.style.WARNING(
                    "Ratsfragen sind pausiert (INSIGHT_QUESTIONS_ENABLED=False) – keine Erinnerungen versendet."
                )
            )
            return

        count = send_due_reminders(days=options["days"], repeat_days=options["repeat"], dry_run=options["dry_run"])
        verb = "fällig" if options["dry_run"] else "gesendet"
        self.stdout.write(self.style.SUCCESS(f"{count} Erinnerung(en) {verb}"))
