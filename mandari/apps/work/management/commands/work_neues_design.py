# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Design in Work je Organisation ein- und ausschalten (Issue #884, Schalter aus #852).

    python manage.py work_neues_design status                       # alle Organisationen
    python manage.py work_neues_design status --org <slug> --json   # maschinenlesbar (Prüfskript)
    python manage.py work_neues_design an --org <slug> --probelauf  # zeigt nur, was sich ändern würde
    python manage.py work_neues_design an --org <slug>
    python manage.py work_neues_design aus --org <slug>             # Rückweg für eine Organisation
    python manage.py work_neues_design aus --alle                   # Rückweg für alle Organisationen

Der Befehl ändert ausschließlich den Schalter (``apps.work.design_schalter.setzen``): keine anderen Einstellungen, keine
Inhalte, keine Mitglieder. Eingeschaltet wird bewusst nur je Organisation (``--org``, mehrfach möglich); „alle auf
einmal“ gibt es nur für den Rückweg. Ablauf beim Ausrollen: docs/WORK_NEUES_DESIGN.md.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.tenants.models import Organization
from apps.work import design_schalter
from apps.work.rahmen import neues_design


class Command(BaseCommand):
    help = "Schaltet das neue Work-Design je Organisation ein oder aus oder zeigt den Stand (nur der Schalter)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("aktion", choices=["status", "an", "aus"], help="status, an oder aus")
        parser.add_argument(
            "--org", action="append", default=[], metavar="SLUG", help="Organisation (Kurzname), mehrfach möglich"
        )
        parser.add_argument("--alle", action="store_true", help="alle Organisationen (nur für status und aus)")
        parser.add_argument("--probelauf", action="store_true", help="nur anzeigen, was sich ändern würde")
        parser.add_argument("--json", action="store_true", help="Stand als JSON ausgeben (nur status)")

    def handle(self, *args: Any, **options: Any) -> None:
        aktion: str = options["aktion"]
        slugs: list[str] = options["org"]
        alle: bool = options["alle"]

        if slugs and alle:
            raise CommandError("Bitte entweder --org oder --alle angeben, nicht beides.")
        if aktion == "an" and alle:
            raise CommandError("Eingeschaltet wird je Organisation: bitte --org <slug> angeben (kein --alle).")
        if aktion != "status" and not (slugs or alle):
            raise CommandError("Bitte --org <slug> angeben (für den Rückweg aller Organisationen: aus --alle).")
        if options["json"] and aktion != "status":
            raise CommandError("--json gibt es nur für status.")

        organisationen = self._organisationen(slugs)

        if aktion == "status":
            self._status(organisationen, als_json=options["json"])
            return

        an = aktion == "an"
        geaendert = 0
        for org in organisationen:
            vorher = neues_design(org)
            if vorher == an:
                self.stdout.write(f"{org.slug}: bereits {self._wort(an)} – nichts zu tun")
                continue
            if options["probelauf"]:
                self.stdout.write(f"{org.slug}: würde von {self._wort(vorher)} auf {self._wort(an)} schalten")
                geaendert += 1
                continue
            if design_schalter.setzen(org, an):
                geaendert += 1
            self.stdout.write(self.style.SUCCESS(f"{org.slug}: neues Design jetzt {self._wort(an)}"))

        if options["probelauf"]:
            self.stdout.write(f"Probelauf: {geaendert} von {len(organisationen)} Organisation(en) würden umgeschaltet.")
        else:
            self.stdout.write(f"{geaendert} von {len(organisationen)} Organisation(en) umgeschaltet.")

    def _organisationen(self, slugs: list[str]) -> list[Organization]:
        if not slugs:
            return list(Organization.objects.order_by("slug"))
        gefunden = {org.slug: org for org in Organization.objects.filter(slug__in=slugs)}
        fehlend = [slug for slug in slugs if slug not in gefunden]
        if fehlend:
            raise CommandError(f"Organisation nicht gefunden: {', '.join(fehlend)}")
        return [gefunden[slug] for slug in dict.fromkeys(slugs)]

    def _status(self, organisationen: list[Organization], *, als_json: bool) -> None:
        if als_json:
            stand = {org.slug: neues_design(org) for org in organisationen}
            self.stdout.write(json.dumps(stand, ensure_ascii=False, sort_keys=True))
            return
        if not organisationen:
            self.stdout.write("Keine Organisationen.")
            return
        for org in organisationen:
            aktiv = "" if org.is_active else " (inaktiv)"
            self.stdout.write(f"{org.slug}{aktiv}: neues Design {self._wort(neues_design(org))}")
        anzahl = sum(neues_design(org) for org in organisationen)
        self.stdout.write(f"Neues Design an: {anzahl} von {len(organisationen)}")

    @staticmethod
    def _wort(an: bool) -> str:
        return "an" if an else "aus"
