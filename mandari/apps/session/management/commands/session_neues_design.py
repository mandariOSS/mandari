# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes je Mandant ein- und ausschalten (Issue #944).

    python manage.py session_neues_design status                          # alle Mandanten
    python manage.py session_neues_design an --mandant <slug> --probelauf # zeigt nur, was sich ändern würde
    python manage.py session_neues_design an --mandant <slug>
    python manage.py session_neues_design aus --mandant <slug>            # Rückweg für einen Mandanten
    python manage.py session_neues_design aus --alle                      # Rückweg für alle Mandanten

Der Befehl ändert ausschließlich den Schalter (``apps.session.rahmen.setzen``): keine Einstellungen, keine Inhalte,
keine Benutzer. Eingeschaltet wird bewusst nur je Mandant (``--mandant``, mehrfach möglich); „alle auf einmal“ gibt es
nur für den Rückweg. Wie ``work_neues_design`` für Work (Issue #884).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.session import rahmen
from apps.session.models import SessionTenant


class Command(BaseCommand):
    help = "Schaltet das neue Erscheinungsbild des Sitzungsdienstes je Mandant ein oder aus oder zeigt den Stand."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("aktion", choices=["status", "an", "aus"], help="status, an oder aus")
        parser.add_argument(
            "--mandant", action="append", default=[], metavar="SLUG", help="Mandant (Kurzname), mehrfach möglich"
        )
        parser.add_argument("--alle", action="store_true", help="alle Mandanten (nur für status und aus)")
        parser.add_argument("--probelauf", action="store_true", help="nur anzeigen, was sich ändern würde")

    def handle(self, *args: Any, **options: Any) -> None:
        aktion: str = options["aktion"]
        slugs: list[str] = options["mandant"]
        alle: bool = options["alle"]
        if slugs and alle:
            raise CommandError("Bitte entweder --mandant oder --alle angeben, nicht beides.")
        if aktion == "an" and alle:
            raise CommandError("Eingeschaltet wird je Mandant: bitte --mandant <slug> angeben (kein --alle).")
        if aktion != "status" and not (slugs or alle):
            raise CommandError("Bitte --mandant <slug> angeben (für den Rückweg aller Mandanten: aus --alle).")

        mandanten = self._mandanten(slugs)
        if aktion == "status":
            for tenant in mandanten:
                aktiv = "" if tenant.is_active else " (inaktiv)"
                self.stdout.write(f"{tenant.slug}{aktiv}: neues Design {self._wort(rahmen.neues_design(tenant))}")
            anzahl = sum(rahmen.neues_design(tenant) for tenant in mandanten)
            self.stdout.write(f"Neues Design an: {anzahl} von {len(mandanten)}")
            return

        an = aktion == "an"
        geaendert = 0
        for tenant in mandanten:
            if rahmen.neues_design(tenant) == an:
                self.stdout.write(f"{tenant.slug}: bereits {self._wort(an)} – nichts zu tun")
                continue
            if options["probelauf"]:
                self.stdout.write(f"{tenant.slug}: würde auf {self._wort(an)} schalten")
                geaendert += 1
                continue
            if rahmen.setzen(tenant, an):
                geaendert += 1
            self.stdout.write(self.style.SUCCESS(f"{tenant.slug}: neues Design jetzt {self._wort(an)}"))
        art = "würden umgeschaltet" if options["probelauf"] else "umgeschaltet"
        self.stdout.write(f"{geaendert} von {len(mandanten)} Mandant(en) {art}.")

    def _mandanten(self, slugs: list[str]) -> list[SessionTenant]:
        if not slugs:
            return list(SessionTenant.objects.order_by("slug"))
        gefunden = {tenant.slug: tenant for tenant in SessionTenant.objects.filter(slug__in=slugs)}
        fehlend = [slug for slug in slugs if slug not in gefunden]
        if fehlend:
            raise CommandError(f"Mandant nicht gefunden: {', '.join(fehlend)}")
        return [gefunden[slug] for slug in dict.fromkeys(slugs)]

    @staticmethod
    def _wort(an: bool) -> str:
        return "an" if an else "aus"
