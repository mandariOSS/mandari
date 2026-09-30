# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Landesprofile für Sitzungsformate anzeigen, prüfen und aus der Profildatei übernehmen (Issue #138).

Die Profile stehen in ``apps/session/presets/landesprofile.json`` und kommen mit der Datenmigration
``session.0045`` in die Datenbank. Ändert ein Release die Datei (neue Rechtslage), übernimmt:

    python manage.py session_state_profiles --sync

    # Abweichungen zwischen Datenbank und Datei zeigen (Exit-Code 1 bei Abweichung)
    python manage.py session_state_profiles --check

    # Übersicht: Land, Regeln für Rat und Ausschüsse, Stand
    python manage.py session_state_profiles
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.session.models import SessionStateProfile
from apps.session.services import meeting_format_service


class Command(BaseCommand):
    help = "Landesprofile für hybride und digitale Sitzungen anzeigen, prüfen oder übernehmen (Issue #138)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--sync", action="store_true", help="Profile aus der Datei übernehmen")
        parser.add_argument("--check", action="store_true", help="Abweichungen zur Datei melden")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["sync"]:
            created, updated = meeting_format_service.sync_profiles()
            self.stdout.write(self.style.SUCCESS(f"Landesprofile übernommen: {created} neu, {updated} aktualisiert."))
            return
        if options["check"]:
            differences = meeting_format_service.profile_differences()
            if differences:
                for line in differences:
                    self.stdout.write(line)
                raise CommandError("Landesprofile weichen von der Profildatei ab – mit --sync übernehmen.")
            self.stdout.write(self.style.SUCCESS("Landesprofile entsprechen der Profildatei."))
            return
        for profile in SessionStateProfile.objects.order_by("name"):
            self.stdout.write(
                f"{profile.code}  {profile.name:<24} hybrid Rat: {profile.get_hybrid_council_display():<24} "
                f"hybrid Ausschüsse: {profile.get_hybrid_committees_display():<24} Stand {profile.as_of:%d.%m.%Y}"
            )
