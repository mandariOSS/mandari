# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fristen-Erinnerungen des Sitzungsdienstes versenden (Issue #83).

Täglicher Lauf (z. B. per Cron):
    python manage.py send_session_reminders

Optionen:
    --tenant <slug>   Nur einen Mandanten bearbeiten
    --dry-run         Nur anzeigen, nichts versenden/protokollieren
"""

from django.core.management.base import BaseCommand

from apps.session.services import reminder_service


class Command(BaseCommand):
    help = "Versendet Fristen-Erinnerungen (Ladung, Vorlagen, Rückmeldung, Beschlusskontrolle)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Nur diesen Mandanten (Slug) bearbeiten")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, welche Erinnerungen versendet würden",
        )

    def handle(self, *args, **options):
        totals = reminder_service.run_all(dry_run=options["dry_run"], tenant_slug=options.get("tenant"))
        if not totals or not any(totals.values()):
            self.stdout.write("Keine fälligen Erinnerungen.")
            return
        for kind, count in sorted(totals.items()):
            if count:
                self.stdout.write(f"{kind}: {count}")
        prefix = "[dry-run] " if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}Erinnerungslauf abgeschlossen."))
