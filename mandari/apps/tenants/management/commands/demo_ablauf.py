# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgelaufene Demo-Organisationen sperren und nach der Frist löschen (Issue #1406, #1575).

Stündlich als Zeitplan im Worker (``apps/common/schedules.py``). Sperrt zuerst (``is_active`` aus, alle Daten
bleiben) und löscht dann gesperrte Demos, deren Ende ``demo.LOESCHFRIST_TAGE`` zurückliegt, endgültig
(höchstens ``demo.LOESCHUNGEN_JE_LAUF`` je Lauf). ``--dry-run`` nennt die fälligen Organisationen, ohne etwas zu
ändern. Scheitert eine Löschung, endet der Befehl mit einem Fehler, nachdem er die übrigen versucht hat.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.common.einmalig import EinmaligMixin
from apps.tenants import demo
from apps.tenants.models import Organization


class Command(EinmaligMixin, BaseCommand):
    sperre = "demo_ablauf"
    sperre_ttl = 600
    help = "Abgelaufene Demo-Organisationen sperren und nach der Frist löschen."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")

    def handle(self, *args: Any, **options: Any) -> None:
        jetzt = timezone.now()
        if options["dry_run"]:
            faellig = Organization.objects.filter(demo.abgelaufen_q(jetzt), Q(is_active=True)).order_by("slug")
            for org in faellig:
                self.stdout.write(f"würde sperren: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
            self.stdout.write(f"{faellig.count()} Demo(s) fällig (Probelauf)")
            loeschen = Organization.objects.filter(demo.loeschfaellig_q(jetzt)).order_by("demo_endet_am", "slug")
            for org in loeschen:
                self.stdout.write(f"würde löschen: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
            self.stdout.write(f"{loeschen.count()} Demo(s) zum Löschen fällig (Probelauf)")
            return
        gesperrt = demo.abgelaufene_sperren(jetzt)
        for org in gesperrt:
            self.stdout.write(f"gesperrt: {org.slug} (Ende {org.demo_endet_am:%d.%m.%Y %H:%M})")
        self.stdout.write(f"{len(gesperrt)} Demo(s) gesperrt")
        lauf = demo.faellige_loeschen(jetzt)
        for geloescht in lauf.geloescht:
            self.stdout.write(
                f"gelöscht: {geloescht.slug} ({geloescht.eintraege} Einträge, {geloescht.dateien} Dateien, "
                f"Konten gelöscht {geloescht.konten_geloescht}, behalten {geloescht.konten_behalten})"
            )
        self.stdout.write(f"{len(lauf.geloescht)} Demo(s) gelöscht")
        if lauf.fehlgeschlagen:
            raise CommandError(f"Löschung fehlgeschlagen: {', '.join(lauf.fehlgeschlagen)}")
